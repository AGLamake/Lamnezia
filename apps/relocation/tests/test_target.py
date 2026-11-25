import hashlib
import os
import shutil
import subprocess
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings

from apps.operations.models import OperationStep
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.relocation.errors import MoveError, MoveErrorCode
from apps.relocation.services.bundle import write_code_bundle
from apps.relocation.services.relay_guard import RelayGuard
from apps.relocation.services.shutdown import STOP_REQUEST
from apps.relocation.services.target import _START_INSTALL, TargetMachine
from apps.ssh.services import CommandResult, RemoteHost
from apps.ssh.tests.fakes import TIMEOUTS, FakeRunner, result

pytestmark = pytest.mark.django_db

PASSWORD = "archive-password-123"
# A free, empty Debian machine with nginx: every check passes.
CLEAN = {
    "[ -f /opt/lamnezia/.moving ]": result(1),
    "[ -e /opt/lamnezia ]": result(1),
    "df -Pm": result(stdout=b"20000\n"),
}


@pytest.fixture
def ctx() -> Iterator[OperationContext]:
    context = OperationContext(OperationFactory())
    with context.step("test"):
        yield context


def machine(
    ctx: OperationContext, answers: dict[str, CommandResult]
) -> tuple[TargetMachine, FakeRunner]:
    runner = FakeRunner(answers)
    return TargetMachine(RemoteHost(runner, TIMEOUTS), ctx, 7), runner


def step_log() -> str:
    return OperationStep.objects.get().log


def commands(runner: FakeRunner) -> str:
    return "\n".join(command for command, _ in runner.calls)


def test_a_clean_machine_passes_the_checks(ctx: OperationContext) -> None:
    target, runner = machine(ctx, CLEAN)

    target.check()

    assert "docker compose --profile caddy down" not in commands(runner)


@pytest.mark.parametrize(
    ("answers", "code"),
    [
        ({"command -v apt-get": result(1)}, MoveErrorCode.UNSUPPORTED_SYSTEM),
        ({"[ -e /opt/lamnezia ]": result(0)}, MoveErrorCode.TARGET_IN_USE),
        ({"df -Pm": result(stdout=b"900\n")}, MoveErrorCode.LOW_DISK),
    ],
)
def test_an_unfit_machine_is_refused(
    answers: dict[str, CommandResult], code: str, ctx: OperationContext
) -> None:
    target, _ = machine(ctx, {**CLEAN, **answers})

    with pytest.raises(MoveError) as error:
        target.check()
    assert error.value.code == code


def own_addresses(*lines: str) -> CommandResult:
    return result(stdout="\n".join(lines).encode())


THE_MACHINE = own_addresses("127.0.0.1/8", "::1/128", "203.0.113.5/24", "172.17.0.1/16")


@pytest.mark.parametrize(
    ("answers", "site_url"),
    [
        # The panel connects to its own public address.
        ({"SSH_CONNECTION": result(stdout=b"203.0.113.5 50000 203.0.113.5 22")}, ""),
        # The panel runs in a container of the machine: it comes from the docker bridge.
        (
            {
                "SSH_CONNECTION": result(stdout=b"172.17.0.3 50000 203.0.113.5 22"),
                "ip -o addr": own_addresses("203.0.113.5/24", "172.17.0.1/16", "172.17.0.3 "),
            },
            "",
        ),
        # The panel domain points to the machine.
        ({}, "https://panel.example.com"),
    ],
)
def test_the_machine_of_the_panel_is_refused_before_any_change(
    answers: dict[str, CommandResult], site_url: str, ctx: OperationContext, settings: Any
) -> None:
    settings.SITE_URL = site_url or "http://localhost:8000"
    machine_addresses = own_addresses("203.0.113.10/24") if site_url else THE_MACHINE
    target, runner = machine(
        ctx,
        {**CLEAN, "[ -f /opt/lamnezia/.moving ]": result(0), "ip -o addr": machine_addresses}
        | answers,
    )

    with pytest.raises(MoveError) as error:
        target.check()
    assert error.value.code == MoveErrorCode.SAME_MACHINE
    assert "docker compose --profile caddy down" not in commands(runner)


def test_another_machine_and_loopback_do_not_count_as_the_panel(
    ctx: OperationContext, settings: Any
) -> None:
    settings.SITE_URL = "http://localhost:8000"
    connection = result(stdout=b"198.51.100.7 50000 203.0.113.5 22")
    target, _ = machine(ctx, {**CLEAN, "SSH_CONNECTION": connection, "ip -o addr": THE_MACHINE})

    target.check()


def test_a_copy_left_by_an_earlier_move_is_removed_first(ctx: OperationContext) -> None:
    target, runner = machine(ctx, {**CLEAN, "[ -f /opt/lamnezia/.moving ]": result(0)})

    target.check()

    assert "docker compose --profile caddy down" in commands(runner)


def test_without_nginx_caddy_needs_ports_80_and_443(ctx: OperationContext) -> None:
    busy = b"udp UNCONN 0 0 0.0.0.0:443 0.0.0.0:*\n"
    target, _ = machine(
        ctx, {**CLEAN, "command -v nginx": result(1), "ss -Hlntu": result(stdout=busy)}
    )

    with pytest.raises(MoveError) as error:
        target.check()
    assert error.value.code == MoveErrorCode.PORTS_BUSY
    assert "443/udp" in str(error.value)


def test_a_relay_on_other_ports_leaves_room_for_caddy(ctx: OperationContext) -> None:
    relay = b"lamnezia-relay 0.0.0.0:51820->51820/udp\n"
    target, _ = machine(
        ctx, {**CLEAN, "command -v nginx": result(1), "docker ps": result(stdout=relay)}
    )

    target.check()


def test_files_go_in_chunks_and_are_checked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ctx: OperationContext
) -> None:
    monkeypatch.setattr("apps.relocation.services.target._CHUNK", 4)
    archive = tmp_path / "panel.lamnezia"
    archive.write_bytes(b"0123456789")
    digest = hashlib.sha256(b"0123456789").hexdigest().encode()
    target, runner = machine(ctx, {"sha256sum": result(stdout=digest + b"  file\n")})

    assert target.upload_archive(archive) == "/var/tmp/lamnezia-move-7/panel.lamnezia"
    assert [stdin for command, stdin in runner.calls if "cat >>" in command] == [
        b"0123",
        b"4567",
        b"89",
    ]

    broken, _ = machine(ctx, {"sha256sum": result(stdout=b"0" * 64)})
    with pytest.raises(MoveError) as error:
        broken.upload_archive(archive)
    assert error.value.code == MoveErrorCode.UPLOAD_FAILED


class InstallerRunner(FakeRunner):
    """Answers the reads of the installer's log with the given outputs, one per read."""

    def __init__(self, reads: list[bytes]) -> None:
        super().__init__()
        self.reads = reads

    def run(self, command: str, *, stdin: bytes = b"", timeout: float | None = None) -> Any:
        if "tail -n +" in command:
            self.calls.append((command, stdin))
            return result(stdout=self.reads.pop(0) if self.reads else b"\n")
        return super().run(command, stdin=stdin, timeout=timeout)


def install(
    ctx: OperationContext, reads: list[bytes], beats: list[int] | None = None
) -> InstallerRunner:
    runner = InstallerRunner(reads)
    counted = beats if beats is not None else []
    target = TargetMachine(RemoteHost(runner, TIMEOUTS), ctx, 7, lambda: counted.append(1))
    target.install("/var/tmp/panel.lamnezia", PASSWORD)
    return runner


def test_the_installer_runs_apart_from_the_ssh_session(ctx: OperationContext) -> None:
    beats: list[int] = []
    runner = install(
        ctx,
        [
            b"\n\x1b[1;34m==>\x1b[0m Restoring the database\nnoi",
            b"0\nnoise\n==> The copy is running\n",
        ],
        beats,
    )

    start, stdin = next(call for call in runner.calls if "mkfifo" in call[0])
    assert "systemd-run --quiet --collect --unit lamnezia-move-7" in start
    assert "setsid -f" in start
    assert '--restore "$2" --moving' in start
    assert stdin == f"{PASSWORD}\n".encode()
    assert all(PASSWORD not in command for command, _ in runner.calls)
    reads = [command for command, _ in runner.calls if "tail -n +" in command]
    assert ("tail -n +1 " in reads[0], "tail -n +2 " in reads[1]) == (True, True)
    assert step_log() == "==> Restoring the database\n==> The copy is running\n"
    assert len(beats) == len(reads)


def test_a_failed_install_shows_its_last_lines(ctx: OperationContext) -> None:
    with pytest.raises(MoveError) as error:
        install(ctx, [b"1\nstep\nError: no space\n"])
    assert error.value.code == MoveErrorCode.INSTALL_FAILED
    assert error.value.detail == "exit 1"
    assert "step\nError: no space" in step_log()


FAKE_INSTALLER = """#!/bin/sh
echo "==> Restoring with $3 $4"
[ "$LAMNEZIA_ARCHIVE_PASSWORD" = "archive-password-123" ] && echo "==> The password came"
exit 3
"""
# Without root or systemd the transient service is a background process like setsid's.
FAKE_SYSTEMD_RUN = '#!/bin/sh\nshift 4\nexec setsid -f "$@"\n'
FAKE_SETSID = '#!/bin/sh\nshift\n"$@" </dev/null >/dev/null 2>&1 &\n'


@pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("sh", "mkfifo", "timeout")),
    reason="no sh, mkfifo or timeout",
)
def test_the_start_script_hands_the_password_over_and_records_the_end(tmp_path: Path) -> None:
    install_dir, work, bin_dir = tmp_path / "opt", tmp_path / "work", tmp_path / "bin"
    for folder in (install_dir, work, bin_dir):
        folder.mkdir()
    (install_dir / "install.sh").write_text(FAKE_INSTALLER, newline="\n")
    for name, body in (("systemd-run", FAKE_SYSTEMD_RUN), ("setsid", FAKE_SETSID)):
        (bin_dir / name).write_text(body, newline="\n")
        (bin_dir / name).chmod(0o755)
    script = _START_INSTALL.format(
        work=work.as_posix(), install_dir=install_dir.as_posix(), unit="u", archive="panel.x"
    )
    environment = {**os.environ, "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}"}

    subprocess.run(
        [shutil.which("sh") or "sh", "-c", script],
        input=f"{PASSWORD}\n".encode(),
        env=environment,
        check=True,
        timeout=30,
    )

    deadline = time.monotonic() + 20
    while not (work / "install.exit").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert (work / "install.exit").read_text().strip() == "3"
    log = (work / "install.log").read_text()
    assert "==> Restoring with --restore panel.x" in log
    assert "==> The password came" in log
    assert not (work / "password").exists()
    assert PASSWORD not in (work / "install.run").read_text()


def test_an_installer_past_its_time_is_stopped(ctx: OperationContext, settings: Any) -> None:
    settings.RELOCATION_INSTALL_TIMEOUT = -1
    runner = InstallerRunner([])
    target = TargetMachine(RemoteHost(runner, TIMEOUTS), ctx, 7)

    with pytest.raises(MoveError) as error:
        target.install("/var/tmp/panel.lamnezia", PASSWORD)
    assert error.value.detail == "timeout"
    assert 'kill -TERM -- "-$pid"' in commands(runner)


def test_the_copy_must_answer_and_hold_the_move(ctx: OperationContext) -> None:
    target, _ = machine(ctx, {"relocation status": result(stdout=b"preparing\n")})
    target.verify("preparing")

    silent, _ = machine(ctx, {"healthz": result(7, stderr=b"refused")})
    with pytest.raises(MoveError) as error:
        silent.verify("preparing")
    assert error.value.code == MoveErrorCode.NOT_STARTED


def test_a_live_copy_must_accept_changes(ctx: OperationContext) -> None:
    target, _ = machine(ctx, {"relocation status": result(stdout=b"arrived\n")})
    target.verify_live()
    assert "accepts changes" in step_log()

    waiting, _ = machine(ctx, {"relocation status": result(stdout=b"preparing\n")})
    with pytest.raises(MoveError) as error:
        waiting.verify_live()
    assert error.value.code == MoveErrorCode.NOT_STARTED


def test_arriving_drops_the_marker_and_a_failed_discard_is_reported(ctx: OperationContext) -> None:
    target, runner = machine(ctx, {})
    target.arrive()
    assert "relocation arrive 7" in commands(runner)
    assert runner.last_command.endswith("'rm -f /opt/lamnezia/.moving'")

    stuck, _ = machine(ctx, {"--profile caddy down": result(1, stderr=b"busy")})
    with pytest.raises(MoveError) as error:
        stuck.discard()
    assert error.value.code == MoveErrorCode.NOT_DISCARDED


class FakeRelay:
    def __init__(self, exists: bool = True, running: bool = True) -> None:
        self.exists = exists
        self.running = running
        self.ports = {51820}
        self.started = 0

    def __call__(self, host: object, ctx: object) -> "FakeRelay":
        return self

    def published_ports(self) -> set[int]:
        return set(self.ports)

    def start_again(self) -> None:
        self.started += 1
        self.running = True


def guard(monkeypatch: pytest.MonkeyPatch, ctx: OperationContext, relay: FakeRelay) -> RelayGuard:
    monkeypatch.setattr("apps.relocation.services.relay_guard.RelayContainer", relay)
    return RelayGuard(object(), ctx)  # type: ignore[arg-type]


def test_a_stopped_relay_is_started_again_with_its_ports(
    monkeypatch: pytest.MonkeyPatch, ctx: OperationContext
) -> None:
    relay = FakeRelay()
    relay_guard = guard(monkeypatch, ctx, relay)
    relay_guard.remember()
    relay.running = False

    relay_guard.check()

    assert relay.started == 1


def test_a_relay_that_lost_its_ports_fails_the_move(
    monkeypatch: pytest.MonkeyPatch, ctx: OperationContext
) -> None:
    relay = FakeRelay()
    relay_guard = guard(monkeypatch, ctx, relay)
    relay_guard.remember()
    relay.ports = set()

    with pytest.raises(MoveError) as error:
        relay_guard.check()
    assert error.value.code == MoveErrorCode.RELAY_BROKEN


def test_without_a_relay_there_is_nothing_to_keep(
    monkeypatch: pytest.MonkeyPatch, ctx: OperationContext
) -> None:
    relay_guard = guard(monkeypatch, ctx, FakeRelay(exists=False, running=False))
    relay_guard.remember()
    relay_guard.check()


def test_the_code_bundle_leaves_out_secrets_and_data(tmp_path: Path) -> None:
    root = tmp_path / "app"
    for name in ("install.sh", "apps/x.py", ".env", ".git/HEAD", "media/a", "apps/__pycache__/x"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x")
    bundle = tmp_path / "code.tar.gz"

    write_code_bundle(root, bundle)

    with tarfile.open(bundle) as archive:
        files = {member.name for member in archive.getmembers() if member.isfile()}
    assert files == {"./install.sh", "./apps/x.py"}


def test_the_installer_knows_the_move_option() -> None:
    script = (settings.BASE_DIR / "install.sh").read_text(encoding="utf-8")

    assert "--moving) MOVING=1" in script
    assert "lamnezia-certificate.timer" in script


def test_the_installer_stops_the_stack_on_the_panel_request() -> None:
    script = (settings.BASE_DIR / "install.sh").read_text(encoding="utf-8")

    assert f"/app/archives/{STOP_REQUEST}" in script
    assert "enable_unit lamnezia-stop.timer" in script
    assert "\n    install_stop_timer\n" in script
