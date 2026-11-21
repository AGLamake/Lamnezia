import os
import shutil
import subprocess
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from amnezia_core.xray import client_fingerprint
from apps.ssh.errors import ErrorCode, SshConnectionError
from apps.ssh.services import RemoteHost
from apps.ssh.tests.fakes import TIMEOUTS, FakeRunner, result
from apps.traffic.models import TrafficCounter, TrafficHourly, TrafficSource
from apps.traffic.services.poller import (
    Channel,
    Dump,
    TrafficPoller,
    dump_script,
    split_dumps,
)
from apps.traffic.services.targets import PeerOwners, PollTarget, PollTargets
from apps.traffic.services.unknown import unknown_sources

DUMP = (Path(__file__).parents[3] / "amnezia_core/tests/fixtures/dump.txt").read_text()
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


def marker_output(*dumps: tuple[str, int, str], started: str = "") -> bytes:
    return "".join(
        f"@@lamnezia-dump {name} {code} {started}\n{text}\n" for name, code, text in dumps
    ).encode()


def target(runner: FakeRunner, containers: dict[str, str], name: str = "nl-1") -> PollTarget:
    host = RemoteHost(runner, TIMEOUTS)
    return PollTarget(name, lambda: nullcontext(host), containers)


def test_split_dumps_by_container() -> None:
    output = marker_output(("amnezia-awg2", 0, DUMP), ("amnezia-awg", 1, "No such container"))

    dumps = split_dumps(output.decode())

    assert dumps["amnezia-awg2"].code == 0
    assert dumps["amnezia-awg2"].text.splitlines() == DUMP.splitlines()
    assert dumps["amnezia-awg"] == Dump(1, "No such container")


def test_split_dumps_reads_the_start_time() -> None:
    output = marker_output(("amnezia-xray", 0, "{}"), started="2026-11-11T10:00:00.5Z")

    assert split_dumps(output.decode())["amnezia-xray"] == Dump(0, "{}", "2026-11-11T10:00:00.5Z")


@pytest.mark.django_db
def test_one_command_per_server_and_failed_container_is_skipped() -> None:
    output = marker_output(("amnezia-awg2", 0, DUMP), ("amnezia-awg", 1, "No such container"))
    runner = FakeRunner({"awg show all dump": result(stdout=output)})

    TrafficPoller(clock=lambda: NOW).poll(
        target(runner, {"awg:1": "amnezia-awg2", "awg:2": "amnezia-awg"})
    )

    assert sum("awg show all dump" in command for command, _ in runner.calls) == 1
    assert set(TrafficCounter.objects.values_list("source", flat=True)) == {"awg:1"}
    assert TrafficCounter.objects.count() == 2


@pytest.mark.django_db
def test_peers_of_a_clone_count_for_the_keys_of_its_leading_server() -> None:
    runner = FakeRunner(
        {"awg show all dump": result(stdout=marker_output(("amnezia-awg2", 0, DUMP)))}
    )
    clone = target(runner, {"awg:2": "amnezia-awg2"})

    TrafficPoller(clock=lambda: NOW).poll(replace(clone, key_sources={"awg:2": "awg:1"}))

    assert set(TrafficCounter.objects.values_list("source", "key_source")) == {("awg:2", "awg:1")}


ALICE = client_fingerprint("6f0c3e52-1b7a-4c8e-9d2f-0a1b2c3d4e5f")


def xray_stats(uplink: int, downlink: int) -> str:
    name = f"user>>>{ALICE}>>>traffic>>>"
    return (
        f'{{"stat": [{{"name": "{name}uplink", "value": {uplink}}},'
        f' {{"name": "{name}downlink", "value": {downlink}}}]}}'
    )


def xray_target(code: int, text: str, started: str = "") -> PollTarget:
    output = marker_output(("amnezia-xray", code, text), started=started)
    return target(FakeRunner({"statsquery": result(stdout=output)}), {"xray:1": "amnezia-xray"})


@pytest.mark.django_db
def test_xray_is_read_in_the_same_command_as_awg() -> None:
    containers = {"awg:1": "amnezia-awg2", "xray:1": "amnezia-xray"}
    for minute, (uplink, downlink) in enumerate(((100, 1000), (300, 5000))):
        output = marker_output(
            ("amnezia-awg2", 0, DUMP), ("amnezia-xray", 0, xray_stats(uplink, downlink))
        )
        runner = FakeRunner({"awg show all dump": result(stdout=output)})

        at = NOW.replace(minute=minute)
        TrafficPoller(clock=lambda: at).poll(target(runner, containers))  # noqa: B023

        assert [command for command, _ in runner.calls if "docker exec" in command] == [
            runner.calls[-1][0]
        ]
        assert "xray api statsquery" in runner.calls[-1][0]
    counter = TrafficCounter.objects.get(source="xray:1")
    assert (counter.public_key, counter.rx_bytes, counter.tx_bytes) == (ALICE, 300, 5000)
    hourly = TrafficHourly.objects.get(source="xray:1")
    assert (hourly.rx_bytes, hourly.tx_bytes) == (200, 4000)
    assert TrafficSource.objects.get(source="xray:1").available


@pytest.mark.django_db
def test_xray_without_statistics_is_unavailable_not_zero() -> None:
    TrafficPoller(clock=lambda: NOW).poll(xray_target(0, xray_stats(100, 1000)))

    TrafficPoller(clock=lambda: NOW.replace(minute=5)).poll(
        xray_target(1, "failed to dial 127.0.0.1:10085")
    )

    assert not TrafficSource.objects.get(source="xray:1").available
    counter = TrafficCounter.objects.get(source="xray:1")
    assert (counter.rx_bytes, counter.tx_bytes) == (100, 1000)


@pytest.mark.django_db
def test_xray_statistics_back_after_a_gap_count_from_zero() -> None:
    TrafficPoller(clock=lambda: NOW).poll(xray_target(0, xray_stats(100, 1000)))
    TrafficPoller(clock=lambda: NOW.replace(minute=5)).poll(xray_target(1, "failed to dial"))

    # The panel put the blocks back and restarted xray: the counters start again.
    TrafficPoller(clock=lambda: NOW.replace(minute=10)).poll(xray_target(0, xray_stats(30, 70)))

    assert TrafficSource.objects.get(source="xray:1").available
    hourly = TrafficHourly.objects.get(source="xray:1")
    assert (hourly.rx_bytes, hourly.tx_bytes) == (30, 70)


@pytest.mark.django_db
def test_xray_restart_from_the_app_between_readings_is_not_undercounted() -> None:
    TrafficPoller(clock=lambda: NOW).poll(xray_target(0, xray_stats(100, 1000), "t1"))
    TrafficPoller(clock=lambda: NOW.replace(minute=5)).poll(
        xray_target(0, xray_stats(5000, 9000), "t2")
    )

    hourly = TrafficHourly.objects.get(source="xray:1")
    assert (hourly.rx_bytes, hourly.tx_bytes) == (5000, 9000)
    assert TrafficCounter.objects.get(source="xray:1").active_at == NOW.replace(minute=5)


@pytest.mark.django_db
def test_unreachable_server_does_not_stop_the_others() -> None:
    def unreachable() -> nullcontext[RemoteHost]:
        raise SshConnectionError(ErrorCode.HOST_UNREACHABLE)

    good = target(
        FakeRunner({"awg show all dump": result(stdout=marker_output(("amnezia-awg2", 0, DUMP)))}),
        {"awg:2": "amnezia-awg2"},
    )
    targets = PollTargets()
    targets.register(lambda: [PollTarget("down", unreachable, {"awg:1": "x"}), good])

    TrafficPoller(clock=lambda: NOW).poll_all(targets)

    assert set(TrafficCounter.objects.values_list("source", flat=True)) == {"awg:2"}


@pytest.mark.django_db
def test_an_unreachable_member_makes_the_traffic_of_the_group_keys_unknown() -> None:
    def unreachable() -> nullcontext[RemoteHost]:
        raise SshConnectionError(ErrorCode.HOST_UNREACHABLE)

    member = PollTarget("down", unreachable, {"awg:2": "amnezia-awg2"}, {"awg:2": "awg:1"})
    targets = PollTargets()
    targets.register(lambda: [member])

    TrafficPoller(clock=lambda: NOW).poll_all(targets)

    assert unknown_sources() == {"awg:1": {"awg:2"}, "awg:2": {"awg:2"}}


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_dump_script_in_a_real_shell(tmp_path: Path) -> None:
    (tmp_path / "dump.txt").write_text(DUMP, newline="\n")
    docker = tmp_path / "docker"
    docker.write_text(
        '#!/bin/sh\n[ "$2" = amnezia-awg2 ] && exec cat "$DUMP_FILE"\n'
        'echo "Error: No such container: $2" >&2; exit 1\n',
        newline="\n",
    )
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    env["DUMP_FILE"] = str(tmp_path / "dump.txt")

    completed = subprocess.run(
        [str(shutil.which("sh")), "-c", dump_script(["amnezia-awg2", "amnezia-awg"])],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    dumps = split_dumps(completed.stdout)
    assert dumps["amnezia-awg2"].code == 0
    assert dumps["amnezia-awg2"].text.splitlines() == DUMP.splitlines()
    assert dumps["amnezia-awg"].code == 1


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_containers_without_awg_are_read_with_wg(tmp_path: Path) -> None:
    # Only amnezia-awg2 has `awg`; the other containers have only `wg`.
    tools = {
        "amnezia-awg2": "awg",
        "amnezia-awg": "wg",
        "amnezia-wireguard": "wg",
        "amnezia-xray": "xray",
    }
    for container, tool in tools.items():
        (tmp_path / container).mkdir()
        (tmp_path / container / tool).write_text(f'#!/bin/sh\necho "{tool} $*"\n', newline="\n")
        (tmp_path / container / tool).chmod(0o755)
    docker = tmp_path / "docker"
    # `docker exec <container> sh -c <script>` runs the script with that container's tools.
    docker.write_text(
        '#!/bin/sh\ncd "$(dirname "$0")/$2" || exit 1\nPATH="$PWD:$PATH" exec sh -c "$5"\n',
        newline="\n",
    )
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    script = dump_script(["amnezia-awg2", "amnezia-awg", "amnezia-wireguard", "amnezia-xray"])

    completed = subprocess.run(
        [str(shutil.which("sh")), "-c", script], env=env, capture_output=True, text=True, check=True
    )

    dumps = split_dumps(completed.stdout)
    assert dumps == {
        "amnezia-awg2": Dump(0, "awg show all dump"),
        "amnezia-awg": Dump(0, "wg show all dump"),
        "amnezia-wireguard": Dump(0, "wg show all dump"),
        "amnezia-xray": Dump(0, "xray api statsquery --server=127.0.0.1:10085 -pattern user>>>"),
    }


ALICE_PEER = "jhkQdvDcgDYudvey3hItwkaQoDy/bLT6QnsyYHwCIlo="
UPLINK_PEER = "Q2+/5jIlpVjLMKDArTagVYSPsI5fcqPNpSem9NMi50s="
SERVER_KEY = "u+xLktHw90auwUVtjZEJVpNCbfbFK8GNmGegY/Z0XYs="


def relay_dump(rx: int, tx: int) -> str:
    """The relay container: a probe, the clone of a split group and its uplink."""
    rows = []
    for interface, peer in (
        ("lr51820", "probe"),
        ("lg51820", ALICE_PEER),
        ("lu51820", "uplink"),
    ):
        rows.append(f"{interface}\tprivate\tpublic\t51820\toff")
        rows.append(
            f"{interface}\t{peer}\t(none)\t203.0.113.7:51820\t10.8.1.1/32\t1793541367"
            f"\t{rx}\t{tx}\toff"
        )
    return "\n".join(rows)


def clone_target(runner: FakeRunner) -> PollTarget:
    return replace(
        target(runner, {"clone:1": "lamnezia-relay"}, name="ru-1 relay"),
        key_sources={"clone:1": "awg:1"},
        interfaces={"clone:1": "lg51820"},
    )


def test_dump_script_reads_the_index_of_an_interface() -> None:
    script = dump_script([Channel("lamnezia-relay", "lg51820")])

    assert "cat /sys/class/net/lg51820/ifindex" in script
    assert "lamnezia-relay@lg51820" in script


@pytest.mark.django_db
def test_an_interface_source_counts_only_its_own_peers() -> None:
    for minute, (rx, tx, started) in enumerate(((100, 1000, "t1/7"), (300, 5000, "t1/7"))):
        output = marker_output(("lamnezia-relay@lg51820", 0, relay_dump(rx, tx)), started=started)
        runner = FakeRunner({"awg show all dump": result(stdout=output)})
        at = NOW.replace(minute=minute)
        TrafficPoller(clock=lambda: at).poll(clone_target(runner))  # noqa: B023

    counter = TrafficCounter.objects.get()
    assert (counter.source, counter.key_source, counter.public_key) == (
        "clone:1",
        "awg:1",
        ALICE_PEER,
    )
    hourly = TrafficHourly.objects.get()
    assert (hourly.source, hourly.rx_bytes, hourly.tx_bytes) == ("awg:1", 200, 4000)


@pytest.mark.django_db
def test_an_interface_created_again_counts_from_zero() -> None:
    readings = ((100, 1000, "t1/7"), (5000, 9000, "t1/12"))
    for minute, (rx, tx, started) in enumerate(readings):
        output = marker_output(("lamnezia-relay@lg51820", 0, relay_dump(rx, tx)), started=started)
        runner = FakeRunner({"awg show all dump": result(stdout=output)})
        at = NOW.replace(minute=minute)
        TrafficPoller(clock=lambda: at).poll(clone_target(runner))  # noqa: B023

    hourly = TrafficHourly.objects.get()
    assert (hourly.rx_bytes, hourly.tx_bytes) == (5000, 9000)


@pytest.mark.django_db
def test_a_missing_interface_leaves_its_peers_absent_not_unknown() -> None:
    output = marker_output(("lamnezia-relay@lg51820", 0, relay_dump(100, 1000)), started="t1/7")
    runner = FakeRunner({"awg show all dump": result(stdout=output)})
    TrafficPoller(clock=lambda: NOW).poll(clone_target(runner))

    gone = marker_output(("lamnezia-relay@lg51820", 0, ""), started="t1/")
    runner = FakeRunner({"awg show all dump": result(stdout=gone)})
    TrafficPoller(clock=lambda: NOW.replace(minute=5)).poll(clone_target(runner))

    assert TrafficSource.objects.get(source="clone:1").available
    assert not TrafficCounter.objects.get().present


@pytest.mark.django_db
def test_a_peer_that_is_not_a_key_counts_for_its_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owners = PeerOwners()
    owners.register(lambda: [(UPLINK_PEER, "uplink:1")])
    monkeypatch.setattr("apps.traffic.services.poller.peer_owners", owners)
    for minute, scale in enumerate((1, 3)):
        dump = DUMP.replace("\t0\t0\t25", f"\t{70 * scale}\t{90 * scale}\t25").replace(
            "1310720\t16148070", f"{10 * scale}\t{20 * scale}"
        )
        runner = FakeRunner(
            {"awg show all dump": result(stdout=marker_output(("amnezia-awg2", 0, dump)))}
        )
        at = NOW.replace(minute=minute)
        member = replace(target(runner, {"awg:2": "amnezia-awg2"}), key_sources={"awg:2": "awg:1"})
        TrafficPoller(clock=lambda: at).poll(member)  # noqa: B023

    rows = set(TrafficHourly.objects.values_list("source", "public_key", "rx_bytes", "tx_bytes"))
    assert rows == {("awg:1", ALICE_PEER, 20, 40), ("uplink:1", UPLINK_PEER, 140, 180)}
    uplink = TrafficCounter.objects.get(public_key=UPLINK_PEER)
    assert (uplink.source, uplink.key_source) == ("awg:2", "uplink:1")


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_interface_dump_in_a_real_shell(tmp_path: Path) -> None:
    (tmp_path / "dump.txt").write_text(relay_dump(1, 2), newline="\n")
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  inspect) echo 2026-11-20T10:00:00Z ;;\n"
        '  exec) if [ "$3" = cat ]; then echo 9; else cat "$DUMP_FILE"; fi ;;\n'
        "esac\n",
        newline="\n",
    )
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    env["DUMP_FILE"] = str(tmp_path / "dump.txt")

    completed = subprocess.run(
        [str(shutil.which("sh")), "-c", dump_script([Channel("lamnezia-relay", "lg51820")])],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    dump = split_dumps(completed.stdout)["lamnezia-relay@lg51820"]
    assert (dump.code, dump.started) == (0, "2026-11-20T10:00:00Z/9")
    assert dump.text.splitlines() == relay_dump(1, 2).splitlines()
