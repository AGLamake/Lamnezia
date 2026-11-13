"""Server-wide lock: `flock` held by a remote process for as long as the lock is needed."""

import shlex
from types import TracebackType

from apps.ssh.errors import ErrorCode, ServerLockError
from apps.ssh.services.commands import CommandRunner, RemoteProcess
from apps.ssh.services.connection import SshTimeouts

_LOCKED_MARKER = "lamnezia-locked"
# flock exit code when the wait times out (-E), distinct from sudo or mkdir failures.
_BUSY_EXIT = 75
_FLOCK_MISSING_EXIT = 127


class ServerLock:
    """Context manager around a remote `flock` that holds the lock until its stdin closes.

    If the worker dies, the SSH session drops, stdin closes and the server frees the lock.
    """

    def __init__(self, runner: CommandRunner, command: str, timeouts: SshTimeouts) -> None:
        self._runner = runner
        self._command = command
        self._timeouts = timeouts
        self._process: RemoteProcess | None = None

    @staticmethod
    def flock_command(path: str, wait: float) -> str:
        holder = f"echo {_LOCKED_MARKER}; exec cat > /dev/null"
        return f"flock -E {_BUSY_EXIT} -w {wait:g} {shlex.quote(path)} sh -c {shlex.quote(holder)}"

    def __enter__(self) -> "ServerLock":
        process = self._runner.start(self._command)
        try:
            line = process.read_line(timeout=self._timeouts.lock + self._timeouts.command)
            if line != _LOCKED_MARKER:
                exit_code = process.wait(timeout=self._timeouts.command)
                raise ServerLockError(_failure_code(exit_code), f"exit {exit_code}")
        except BaseException:
            process.close()
            raise
        self._process = process
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._process is None:
            return
        process, self._process = self._process, None
        try:
            process.close_stdin()
            process.wait(timeout=self._timeouts.command)
        finally:
            process.close()


def _failure_code(exit_code: int) -> ErrorCode:
    if exit_code == _BUSY_EXIT:
        return ErrorCode.SERVER_BUSY
    if exit_code == _FLOCK_MISSING_EXIT:
        return ErrorCode.FLOCK_NOT_INSTALLED
    return ErrorCode.COMMAND_FAILED
