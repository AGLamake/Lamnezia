"""A fake host for the TSO/GSO workaround: containers, their interfaces and the systemd timer."""

import shlex
from dataclasses import dataclass, field

from apps.protocols.services import offload_scripts as scripts
from apps.ssh.services import CommandResult, CommandRunner, RemoteHost, RemoteProcess
from apps.ssh.tests.fakes import TIMEOUTS, FakeProcess, result


@dataclass
class Interface:
    name: str = "eth1"
    tso: str = "on"
    gso: str = "on"


@dataclass
class FakeOffloadHost(CommandRunner):
    """A Debian host that answers the scripts of offload_scripts.py as the real ones would.

    `interfaces` are the outgoing interfaces of the running containers, by container name.
    """

    systemd: bool = True
    commands: set[str] = field(default_factory=lambda: {"ip", "nsenter"})
    apt_fails: bool = False
    interfaces: dict[str, Interface] = field(default_factory=lambda: {"amnezia-awg2": Interface()})
    files: dict[str, bytes] = field(default_factory=dict)
    timer_active: bool = False
    installed: list[str] = field(default_factory=list)
    locks: int = 0

    def host(self) -> RemoteHost:
        return RemoteHost(self, TIMEOUTS)

    def restart(self, container: str) -> None:
        """Docker recreates the interface with the offloads on."""
        self.interfaces[container] = Interface(self.interfaces[container].name)

    def tick(self) -> None:
        """A run of the timer: the service runs the installed script."""
        if self.timer_active and scripts.SERVICE_PATH in self.files:
            self._apply()

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        if command == "id -u":
            return result(stdout=b"0\n")
        argv = shlex.split(command)
        assert argv[:2] == ["sh", "-c"], command
        return self._script(argv[2], stdin)

    def start(self, command: str) -> RemoteProcess:
        self.locks += 1
        return FakeProcess(["lamnezia-locked"])

    def _script(self, script: str, stdin: bytes) -> CommandResult:  # noqa: PLR0911
        if script == scripts.HAS_SYSTEMD:
            return result(0 if self.systemd else 1)
        if script == scripts.MISSING_COMMANDS:
            missing = [name for name in scripts.PACKAGES if name not in self.commands]
            return result(stdout="".join(f"{name}\n" for name in missing).encode())
        if "apt-get" in script:
            return self._apt(script)
        if "target=" in script:
            self.files[shlex.split(script.split("target=", 1)[1])[0]] = stdin
            return result()
        if script == scripts.ENABLE:
            assert self.files[scripts.TIMER_PATH] == scripts.TIMER.encode()
            self.timer_active = True
            return result(stdout=self._apply().encode())
        if script == scripts.DISABLE:
            return self._disable()
        if script == scripts.STATUS:
            return result(stdout=self._status().encode())
        raise AssertionError(f"unexpected script: {script}")

    def _apt(self, script: str) -> CommandResult:
        if self.apt_fails:
            return result(100, stderr=b"E: Unable to locate package ethtool\n")
        for command, package in scripts.PACKAGES.items():
            if f" {package}" in script:
                self.installed.append(package)
                self.commands.add(command)
        return result()

    def _apply(self) -> str:
        lines = []
        for name, interface in self.interfaces.items():
            if "on" in (interface.tso, interface.gso):
                interface.tso = interface.gso = "off"
                lines.append(f"{name} {interface.name}: TSO/GSO turned off\n")
        return "".join(lines)

    def _disable(self) -> CommandResult:
        self.timer_active = False
        for path in (scripts.SERVICE_PATH, scripts.TIMER_PATH, scripts.SCRIPT_PATH):
            self.files.pop(path, None)
        if "ethtool" in self.commands:
            for interface in self.interfaces.values():
                interface.tso = interface.gso = "on"
        return result()

    def _status(self) -> str:
        lines = [f"timer {'active' if self.timer_active else 'inactive'}\n"]
        for name, interface in self.interfaces.items():
            tso, gso = (interface.tso, interface.gso) if "ethtool" in self.commands else ("-", "-")
            lines.append(f"{name} {interface.name} {tso} {gso}\n")
        return "".join(lines)
