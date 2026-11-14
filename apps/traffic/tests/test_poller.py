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
from apps.traffic.services.poller import Dump, TrafficPoller, dump_script, split_dumps
from apps.traffic.services.targets import PollTarget, PollTargets

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
