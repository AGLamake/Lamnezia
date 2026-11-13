import pytest

from apps.ssh.errors import ErrorCode, ServerLockError
from apps.ssh.services import RemoteHost
from apps.ssh.tests.fakes import TIMEOUTS, FakeProcess, FakeRunner


def test_lock_holds_flock_until_exit() -> None:
    runner = FakeRunner()
    host = RemoteHost(runner, TIMEOUTS)

    with host.lock():
        assert not runner.process.stdin_closed

    assert runner.process.stdin_closed
    assert runner.process.closed


def test_lock_command_uses_amnezia_lock_file_and_wait_timeout() -> None:
    runner = FakeRunner()

    with RemoteHost(runner, TIMEOUTS).lock():
        pass

    command = runner.started[0]
    assert "flock -E 75 -w 5" in command
    assert "/opt/amnezia/.lamnezia.lock" in command


def test_lock_is_released_when_body_fails() -> None:
    runner = FakeRunner()

    with pytest.raises(RuntimeError), RemoteHost(runner, TIMEOUTS).lock():
        raise RuntimeError

    assert runner.process.stdin_closed


@pytest.mark.parametrize(
    ("exit_code", "code"),
    [
        (75, ErrorCode.SERVER_BUSY),
        (127, ErrorCode.FLOCK_NOT_INSTALLED),
        (1, ErrorCode.COMMAND_FAILED),
    ],
)
def test_lock_failures(exit_code: int, code: ErrorCode) -> None:
    runner = FakeRunner()
    runner.process = FakeProcess([], exit_code=exit_code)

    with pytest.raises(ServerLockError) as error, RemoteHost(runner, TIMEOUTS).lock():
        pass

    assert error.value.code == code
    assert runner.process.closed
