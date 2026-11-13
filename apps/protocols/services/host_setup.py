"""Preparing a fresh server for Amnezia containers, the same way the Amnezia app does it."""

import shlex
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Self

from django.utils.translation import gettext as _

from apps.operations.services.context import OperationContext
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services import host_scripts as scripts
from apps.ssh.services import CommandResult, RemoteHost

# Lines of apt output kept in the step log when the installation fails.
_LOG_TAIL = 20


@dataclass(frozen=True)
class OsRelease:
    """The parts of /etc/os-release that decide whether the panel can prepare the server."""

    id: str
    id_like: tuple[str, ...]
    pretty_name: str

    @classmethod
    def parse(cls, text: str) -> Self:
        values: dict[str, str] = {}
        for line in text.splitlines():
            key, sep, raw = line.partition("=")
            if sep and not key.startswith("#"):
                words = shlex.split(raw, comments=False) if raw.strip() else []
                values[key.strip()] = " ".join(words)
        return cls(
            id=values.get("ID", "").lower(),
            id_like=tuple(values.get("ID_LIKE", "").lower().split()),
            pretty_name=values.get("PRETTY_NAME") or values.get("ID", "unknown"),
        )

    @property
    def is_supported(self) -> bool:
        return bool(scripts.SUPPORTED_DISTRIBUTIONS & {self.id, *self.id_like})


class HostSetup:
    """Installs Docker, creates the Amnezia network and tunes the firewall and the kernel.

    Every step looks at the server first and changes only what is missing, so running the
    operation again is safe. Changes happen under the server lock (DESIGN.md 5.4).
    """

    def __init__(self, host: RemoteHost, ctx: OperationContext) -> None:
        self._host = host
        self._ctx = ctx

    def run(self) -> None:
        self.check_os()
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            self.prepare()

    def check_os(self) -> None:
        """Refuse an unsupported server before anything is changed on it."""
        with self._ctx.step(_("Check the operating system")):
            self._check_os()

    def prepare(self) -> None:
        """The changing steps; the caller holds the server lock."""
        with self._ctx.step(_("Install Docker")):
            self._install_packages()
        with self._ctx.step(_("Start Docker")):
            self._start_docker()
        with self._ctx.step(_("Create the Amnezia network")):
            self._create_network()
        with self._ctx.step(_("Configure the firewall and network settings")):
            self._tune_network()

    def install_conntrack(self) -> None:
        """conntrack for the Telegram proxies; upstream goes on without it, so does the panel."""
        with self._ctx.step(_("Install conntrack")):
            if self._host.sh("command -v conntrack >/dev/null 2>&1").ok:
                self._ctx.log(_("conntrack is already installed."))
                return
            command = scripts.install_packages([scripts.CONNTRACK_PACKAGE])
            result = self._host.sh(command, timeout=scripts.APT_TIMEOUT)
            if not result.ok:
                self._log_tail(result)
                self._ctx.log(_("conntrack could not be installed; the installation goes on."))

    def _check_os(self) -> None:
        release = OsRelease.parse(self._host.read_file(scripts.OS_RELEASE_PATH).decode())
        self._ctx.log(f"{release.pretty_name}, {self._host.run('uname -sr').strip()}")
        if not release.is_supported:
            raise SetupError(SetupErrorCode.UNSUPPORTED_OS, release.pretty_name)

    def _install_packages(self) -> None:
        missing = self._missing_commands()
        if not missing:
            self._ctx.log(_("Everything is already installed."))
            return
        packages = [scripts.PACKAGES[command] for command in missing]
        self._ctx.log(_("Installing: %(packages)s") % {"packages": " ".join(packages)})
        result = self._host.sh(scripts.install_packages(packages), timeout=scripts.APT_TIMEOUT)
        if not result.ok:
            self._log_tail(result)
            raise SetupError(_apt_error(result), f"exit {result.exit_code}")
        if "docker" in self._missing_commands():
            raise SetupError(SetupErrorCode.PACKAGE_INSTALL_FAILED, "docker not found")

    def _start_docker(self) -> None:
        state = self._host.sh(scripts.DOCKER_SERVICE).output.strip().splitlines()
        if state[-1:] != [scripts.DOCKER_ACTIVE]:
            raise SetupError(SetupErrorCode.DOCKER_NOT_RUNNING, " ".join(state))
        self._ctx.log(self._host.docker("--version").strip())

    def _create_network(self) -> None:
        network = {"name": scripts.AMNEZIA_NETWORK}
        names = self._host.docker("network", "ls", "--format", "{{.Name}}").split()
        if scripts.AMNEZIA_NETWORK in names:
            self._ctx.log(_("The network %(name)s already exists.") % network)
            return
        self._host.docker(*scripts.AMNEZIA_NETWORK_CREATE)
        self._ctx.log(_("The network %(name)s is created.") % network)

    def _tune_network(self) -> None:
        commands = scripts.network_tuning_commands()
        skipped = self._host.run(scripts.tolerant(commands)).splitlines()
        for command in skipped:
            self._ctx.log(
                _("Not supported by the server, skipped: %(command)s") % {"command": command}
            )
        self._ctx.log(
            _("Applied: %(count)d of %(total)d.")
            % {"count": len(commands) - len(skipped), "total": len(commands)}
        )

    def _missing_commands(self) -> list[str]:
        return self._host.run(scripts.MISSING_COMMANDS).split()

    def _log_tail(self, result: CommandResult) -> None:
        log_tail(self._ctx, result)


def log_tail(ctx: OperationContext, result: CommandResult) -> None:
    """Last lines of a failed command's output, so the step log shows why it failed."""
    for line in (result.output + result.errors).strip().splitlines()[-_LOG_TAIL:]:
        ctx.log(line)


def _apt_error(result: CommandResult) -> SetupErrorCode:
    text = result.output + result.errors
    if any(message in text for message in scripts.APT_LOCK_MESSAGES):
        return SetupErrorCode.PACKAGE_MANAGER_BUSY
    return SetupErrorCode.PACKAGE_INSTALL_FAILED
