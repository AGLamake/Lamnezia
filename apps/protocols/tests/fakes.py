from collections.abc import Callable
from dataclasses import dataclass, field

from apps.ssh.services import CommandResult, CommandRunner, RemoteHost, RemoteProcess
from apps.ssh.tests.fakes import TIMEOUTS, FakeProcess, result

DEBIAN_12 = b'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\n'
UBUNTU_24 = b'PRETTY_NAME="Ubuntu 24.04.1 LTS"\nID=ubuntu\nID_LIKE=debian\n'


@dataclass
class FakeDebianServer(CommandRunner):
    """A Debian server that remembers what is installed, so a second setup can be checked."""

    os_release: bytes = DEBIAN_12
    commands: set[str] = field(default_factory=set)
    apparmor: bool = False
    networks: set[str] = field(default_factory=lambda: {"bridge", "host", "none"})
    docker_starts: bool = True
    apt_error: bytes = b""
    unsupported_settings: tuple[str, ...] = ("tcp_tw_recycle",)
    installed: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    locks: int = 0

    def host(self) -> RemoteHost:
        return RemoteHost(self, TIMEOUTS)

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        answers: tuple[tuple[str, Callable[[], CommandResult]], ...] = (
            ("id -u", lambda: result(stdout=b"0\n")),
            ("cat -- /etc/os-release", lambda: result(stdout=self.os_release)),
            ("uname -sr", lambda: result(stdout=b"Linux 6.1.0-28-amd64\n")),
            ("command -v", self._missing),
            ("apt-get", lambda: self._apt(command)),
            ("systemctl", self._docker_service),
            ("docker --version", lambda: result(stdout=b"Docker version 26.1.5\n")),
            ("docker network ls", lambda: result(stdout="\n".join(self.networks).encode())),
            ("docker network create", lambda: self._create_network(command)),
            ("iptables", lambda: self._tune(command)),
        )
        for needle, answer in answers:
            if needle in command:
                return answer()
        raise AssertionError(f"unexpected command: {command}")

    def start(self, command: str) -> RemoteProcess:
        self.locks += 1
        return FakeProcess(["lamnezia-locked"])

    def _missing(self) -> CommandResult:
        wanted = ["docker", "fuser", "lsof"] + (["apparmor_parser"] if self.apparmor else [])
        missing = [name for name in wanted if name not in self.commands]
        return result(stdout="".join(f"{name}\n" for name in missing).encode())

    def _apt(self, command: str) -> CommandResult:
        if self.apt_error:
            return result(100, stderr=self.apt_error)
        provides = {
            "docker.io": "docker",
            "psmisc": "fuser",
            "lsof": "lsof",
            "apparmor": "apparmor_parser",
        }
        for package, binary in provides.items():
            if f" {package}" in command:
                self.installed.append(package)
                self.commands.add(binary)
        return result()

    def _docker_service(self) -> CommandResult:
        return result(stdout=b"active\n" if self.docker_starts else b"failed\n")

    def _create_network(self, command: str) -> CommandResult:
        assert "--subnet=172.29.172.0/24" in command
        assert "com.docker.network.bridge.name=amn0" in command
        self.networks.add("amnezia-dns-net")
        self.changes.append("network")
        return result()

    def _tune(self, command: str) -> CommandResult:
        self.changes.append("tuning")
        # The real script prints each failed command; the fake prints the unsupported names.
        skipped = [name for name in self.unsupported_settings if name in command]
        return result(stdout="".join(f"{name}\n" for name in skipped).encode())
