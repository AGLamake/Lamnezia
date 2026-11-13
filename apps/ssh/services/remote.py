"""Root shell on a server and in its Docker containers: commands, file reads, atomic writes."""

import shlex
from abc import ABC, abstractmethod

from apps.ssh.errors import (
    DockerError,
    ErrorCode,
    RemoteFileNotFoundError,
    SshCommandError,
    SshPrivilegeError,
)
from apps.ssh.services.commands import CommandResult, CommandRunner
from apps.ssh.services.connection import SshTimeouts
from apps.ssh.services.lock import ServerLock

# Exit code our read script uses for "no such file" (EX_NOINPUT), so it is not confused with errors.
_FILE_MISSING_EXIT = 66
# The shell exits with 127 for a missing binary.
_COMMAND_NOT_FOUND_EXIT = 127
# Prefix of errors from the docker daemon; its exit code differs between docker versions (1 or 125).
_DAEMON_ERROR = "Error response from daemon"
_DETAIL_LIMIT = 2000


class ShellTarget(ABC):
    """A place where shell scripts run as root: the server itself or a container on it."""

    @abstractmethod
    def sh(self, script: str, *, stdin: bytes = b"", timeout: float | None = None) -> CommandResult:
        """Run a POSIX sh script as root and return the result, whatever the exit code."""

    def run(self, script: str, *, stdin: bytes = b"", timeout: float | None = None) -> str:
        """Run a script that must succeed; return its stdout."""
        result = self.sh(script, stdin=stdin, timeout=timeout)
        if not result.ok:
            raise SshCommandError(ErrorCode.COMMAND_FAILED, _describe(result))
        return result.output

    def read_file(self, path: str) -> bytes:
        quoted = shlex.quote(path)
        result = self.sh(f"[ -f {quoted} ] || exit {_FILE_MISSING_EXIT}; cat -- {quoted}")
        if result.exit_code == _FILE_MISSING_EXIT:
            raise RemoteFileNotFoundError(path)
        if not result.ok:
            raise SshCommandError(ErrorCode.COMMAND_FAILED, _describe(result))
        return result.stdout

    def write_file_atomic(self, path: str, data: bytes, mode: int = 0o600) -> None:
        """Write via a temporary file in the same directory and rename it over the target.

        Readers (the Amnezia app included) see either the old or the new file, never a half.
        """
        script = (
            "set -e\n"
            f"target={shlex.quote(path)}\n"
            'dir=$(dirname -- "$target")\n'
            'mkdir -p -- "$dir"\n'
            'tmp=$(mktemp "$dir/.lamnezia.XXXXXX")\n'
            "trap 'rm -f -- \"$tmp\"' EXIT\n"
            'cat > "$tmp"\n'
            f'chmod {mode:o} "$tmp"\n'
            'mv -f -- "$tmp" "$target"\n'
        )
        self.run(script, stdin=data)


class RemoteHost(ShellTarget):
    """The server: root commands through sudo when needed, containers and the server lock."""

    LOCK_DIR = "/opt/amnezia"
    LOCK_PATH = f"{LOCK_DIR}/.lamnezia.lock"

    def __init__(self, runner: CommandRunner, timeouts: SshTimeouts) -> None:
        self._runner = runner
        self._timeouts = timeouts
        self._sudo_prefix: str | None = None

    def check_privileges(self) -> None:
        """Make sure the user is root or has passwordless sudo, as the Amnezia app requires."""
        if self._runner.run("id -u").output.strip() == "0":
            self._sudo_prefix = ""
            return
        result = self._runner.run("sudo -n true")
        if not result.ok:
            groups = self._runner.run("id -nG").output.split()
            raise SshPrivilegeError(_privilege_error(result.errors, groups), result.errors)
        self._sudo_prefix = "sudo -n "

    def sh(self, script: str, *, stdin: bytes = b"", timeout: float | None = None) -> CommandResult:
        return self._runner.run(self._as_root(script), stdin=stdin, timeout=timeout)

    def docker(self, *args: str) -> str:
        """Run a docker CLI command on the server and return its stdout."""
        result = self.sh(shlex.join(["docker", *args]))
        _raise_for_docker(result)
        if not result.ok:
            raise SshCommandError(ErrorCode.COMMAND_FAILED, _describe(result))
        return result.output

    def container(self, name: str) -> "DockerContainer":
        return DockerContainer(self, name)

    def lock(self) -> ServerLock:
        """Server-wide lock shared by all panel workers (DESIGN.md 5.4)."""
        script = (
            f"mkdir -p -- {shlex.quote(self.LOCK_DIR)} && "
            f"exec {ServerLock.flock_command(self.LOCK_PATH, self._timeouts.lock)}"
        )
        return ServerLock(self._runner, self._as_root(script), self._timeouts)

    def _as_root(self, script: str) -> str:
        if self._sudo_prefix is None:
            self.check_privileges()
        return f"{self._sudo_prefix}sh -c {shlex.quote(script)}"


class DockerContainer(ShellTarget):
    """A container on the server; scripts run inside it with `docker exec`."""

    def __init__(self, host: RemoteHost, name: str) -> None:
        self._host = host
        self.name = name

    def sh(self, script: str, *, stdin: bytes = b"", timeout: float | None = None) -> CommandResult:
        command = shlex.join(["docker", "exec", "-i", self.name, "sh", "-c", script])
        result = self._host.sh(command, stdin=stdin, timeout=timeout)
        _raise_for_docker(result)
        return result


def _raise_for_docker(result: CommandResult) -> None:
    errors = result.errors
    daemon_failed = not result.ok and _DAEMON_ERROR in errors
    if daemon_failed and "No such container" in errors:
        raise DockerError(ErrorCode.CONTAINER_NOT_FOUND, errors)
    if daemon_failed and "is not running" in errors:
        raise DockerError(ErrorCode.CONTAINER_NOT_RUNNING, errors)
    if result.exit_code == _COMMAND_NOT_FOUND_EXIT and "docker" in errors:
        raise DockerError(ErrorCode.DOCKER_NOT_INSTALLED, errors)


def _privilege_error(sudo_errors: str, groups: list[str]) -> ErrorCode:
    if "not found" in sudo_errors:
        return ErrorCode.SUDO_NOT_INSTALLED
    # sudo -n reports "password is required" even for users outside sudoers, so groups decide.
    if "password is required" in sudo_errors and {"sudo", "wheel"} & set(groups):
        return ErrorCode.SUDO_PASSWORD_REQUIRED
    return ErrorCode.USER_NOT_IN_SUDOERS


def _describe(result: CommandResult) -> str:
    return f"exit {result.exit_code}: {result.errors.strip()[:_DETAIL_LIMIT]}"
