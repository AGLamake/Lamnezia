"""What the rest of the SSH layer needs from a connection: run a command, start a long one."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import IO


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: bytes
    stderr: bytes

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        return self.stdout.decode(errors="replace")

    @property
    def errors(self) -> str:
        return self.stderr.decode(errors="replace")


class RemoteProcess(ABC):
    """A command that keeps running while the caller talks to it."""

    @abstractmethod
    def read_line(self, timeout: float) -> str | None:
        """Next stdout line without the newline; None when the command ended first."""

    @abstractmethod
    def close_stdin(self) -> None: ...

    @abstractmethod
    def wait(self, timeout: float) -> int:
        """Wait for the command to end and return its exit code."""

    @abstractmethod
    def close(self) -> None: ...


class CommandRunner(ABC):
    """Runs shell commands on a server. Implemented by SshConnection and by fakes in tests."""

    @abstractmethod
    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        """Run the command to the end. Stdin data never appears in errors or logs."""

    def run_into(
        self, command: str, sink: IO[bytes], *, timeout: float | None = None
    ) -> CommandResult:
        """Run the command, writing its stdout into `sink`; the result holds no stdout.

        A connection writes the output as it comes, so a large one is never all in memory.
        """
        result = self.run(command, timeout=timeout)
        sink.write(result.stdout)
        return replace(result, stdout=b"")

    @abstractmethod
    def start(self, command: str) -> RemoteProcess: ...
