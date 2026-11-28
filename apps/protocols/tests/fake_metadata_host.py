"""A fake host for the cloud metadata block: the Docker bridges, the rule and the systemd timer."""

import shlex
from dataclasses import dataclass, field

from apps.protocols.services import metadata_scripts as scripts
from apps.ssh.services import CommandResult, CommandRunner, RemoteHost, RemoteProcess, Stdin
from apps.ssh.tests.fakes import TIMEOUTS, FakeProcess, result, stdin_bytes

METADATA_PATHS = frozenset(path for path, _content, _mode in scripts.FILES)


def without_metadata_block[T](files: dict[str, T]) -> dict[str, T]:
    """The files of a fake server besides those the metadata block writes after an import."""
    return {path: content for path, content in files.items() if path not in METADATA_PATHS}


def amnezia_writes(commands: list[str]) -> list[str]:
    """The file writes of a fake server besides those of the metadata block."""
    return [
        command
        for command in commands
        if "target=" in command and not any(path in command for path in METADATA_PATHS)
    ]


@dataclass
class FakeMetadataHost(CommandRunner):
    """A Debian host that answers the scripts of metadata_scripts.py as the real ones would.

    `bridges` are the interfaces of the Docker networks of the Amnezia containers; `closed` are
    those the chain of the block drops 169.254.0.0/16 from, and `jumped` says whether
    DOCKER-USER leads to the chain.
    """

    systemd: bool = True
    bridges: tuple[str, ...] = ("docker0", "br-1a2b3c4d5e6f")
    files: dict[str, bytes] = field(default_factory=dict)
    closed: list[str] = field(default_factory=list)
    jumped: bool = False
    timer_active: bool = False
    locks: int = 0

    def host(self) -> RemoteHost:
        return RemoteHost(self, TIMEOUTS)

    def reboot(self) -> None:
        """The rules of iptables are gone; the units stay on the disk."""
        self.closed = []
        self.jumped = False

    def tick(self) -> None:
        """A run of the timer: the service runs the installed script."""
        if self.timer_active and scripts.SERVICE_PATH in self.files:
            self._apply()

    def run(
        self, command: str, *, stdin: Stdin = b"", timeout: float | None = None
    ) -> CommandResult:
        stdin = stdin_bytes(stdin)
        if command == "id -u":
            return result(stdout=b"0\n")
        argv = shlex.split(command)
        assert argv[:2] == ["sh", "-c"], command
        return self._script(argv[2], stdin)

    def start(self, command: str) -> RemoteProcess:
        self.locks += 1
        return FakeProcess(["lamnezia-locked"])

    def _script(self, script: str, stdin: bytes) -> CommandResult:
        if script == scripts.HAS_SYSTEMD:
            return result(0 if self.systemd else 1)
        if "target=" in script:
            self.files[shlex.split(script.split("target=", 1)[1])[0]] = stdin
            return result()
        if script == scripts.ENABLE:
            assert self.files[scripts.TIMER_PATH] == scripts.TIMER.encode()
            assert self.files[scripts.SCRIPT_PATH] == scripts.SCRIPT.encode()
            self.timer_active = True
            return result(stdout=self._apply().encode())
        if script == scripts.DISABLE:
            self.timer_active = False
            self.reboot()
            for path, _content, _mode in scripts.FILES:
                self.files.pop(path, None)
            return result()
        if script == scripts.STATUS:
            return result(stdout=self._status().encode())
        raise AssertionError(f"unexpected script: {script}")

    def _apply(self) -> str:
        lines = []
        for bridge in self.bridges:
            if bridge not in self.closed:
                self.closed.append(bridge)
                lines.append(f"{bridge}: {scripts.LINK_LOCAL} closed\n")
        if not self.jumped:
            self.jumped = True
            lines.append(f"DOCKER-USER: {scripts.CHAIN} added\n")
        return "".join(lines)

    def _status(self) -> str:
        lines = [
            f"timer {'active' if self.timer_active else 'inactive'}\n",
            f"jump {'yes' if self.jumped else 'no'}\n",
        ]
        lines += [f"closed {bridge}\n" for bridge in self.closed]
        return "".join(lines)
