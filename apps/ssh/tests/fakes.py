from apps.ssh.services import CommandResult, CommandRunner, RemoteProcess, SshTimeouts

TIMEOUTS = SshTimeouts(connect=1, command=1, lock=5)


def result(exit_code: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> CommandResult:
    return CommandResult(exit_code, stdout, stderr)


class FakeProcess(RemoteProcess):
    def __init__(self, lines: list[str], exit_code: int = 0) -> None:
        self.lines = lines
        self.exit_code = exit_code
        self.stdin_closed = False
        self.closed = False

    def read_line(self, timeout: float) -> str | None:
        return self.lines.pop(0) if self.lines else None

    def close_stdin(self) -> None:
        self.stdin_closed = True

    def wait(self, timeout: float) -> int:
        return self.exit_code

    def close(self) -> None:
        self.closed = True


class FakeRunner(CommandRunner):
    """Answers commands by the first matching substring; records what was run."""

    def __init__(self, answers: dict[str, CommandResult] | None = None) -> None:
        self.answers = {"id -u": result(stdout=b"0\n"), **(answers or {})}
        self.calls: list[tuple[str, bytes]] = []
        self.process = FakeProcess(["lamnezia-locked"])
        self.started: list[str] = []

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        self.calls.append((command, stdin))
        for needle, answer in self.answers.items():
            if needle in command:
                return answer
        return result()

    def start(self, command: str) -> RemoteProcess:
        self.started.append(command)
        return self.process

    @property
    def last_command(self) -> str:
        return self.calls[-1][0]
