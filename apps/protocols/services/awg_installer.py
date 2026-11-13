"""Installing the WireGuard-family containers with the upstream server scripts.

AmneziaWG 2.0 (server_scripts/awg/), AmneziaWG legacy (awg_legacy/) and WireGuard (wireguard/).
"""

import re
from collections.abc import Mapping
from typing import Any, ClassVar, Self

from django.utils.translation import gettext as _

from amnezia_core.awg import AwgServerConfig
from amnezia_core.awg_backup import AwgServerFiles
from amnezia_core.awg_install import DEFAULT_PORT, WIREGUARD_DEFAULT_PORT, AwgInstallSettings
from amnezia_core.clients_table import ClientsTable
from amnezia_core.containers import AWG2, AWG_LEGACY, WIREGUARD, WgContainer
from amnezia_core.keys import KeyPair
from apps.operations.services.context import OperationContext
from apps.protocols.catalog import CATALOG
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.awg_container import SCRIPT_FOLDERS, AwgContainer, run_arguments
from apps.protocols.services.container import Transport
from apps.protocols.services.installer import ContainerInstaller, installers
from apps.ssh.services import RemoteHost

# AmneziaWG 2.0 needs Linux 4.14 or newer (upstream ServerLinuxKernelTooOld).
_MIN_KERNEL = (4, 14)
_KERNEL_VERSION = re.compile(r"^(\d+)\.(\d+)")


class WgInstaller(ContainerInstaller[AwgServerConfig]):
    """Builds and starts one WireGuard-family container, writes its config, clientsTable, start.sh.

    A subclass names the container in `kind`. Files already in the container are never
    overwritten. `files` restore a server with its saved keys instead of generating new ones.
    `logs` keeps the container log, which Amnezia turns off.
    """

    kind: ClassVar[WgContainer]
    transport = Transport.UDP
    install_fields = ("offload",)

    def __init__(
        self,
        host: RemoteHost,
        ctx: OperationContext,
        settings: AwgInstallSettings,
        files: AwgServerFiles | None = None,
        *,
        logs: bool = False,
    ) -> None:
        self._awg = AwgContainer(host, ctx, self.kind)
        super().__init__(host, ctx, self._awg, port=settings.port, logs=logs)
        self._settings = settings
        self._files = files

    @classmethod
    def from_params(
        cls, host: RemoteHost, ctx: OperationContext, params: Mapping[str, Any]
    ) -> Self:
        settings = AwgInstallSettings.for_container(cls.kind, int(params["port"]))
        return cls(host, ctx, settings, logs=bool(params["logs"]))

    def script_variables(self) -> dict[str, str]:
        return self._settings.script_variables()

    def run_arguments(self) -> list[str]:
        return run_arguments(self.port, logs=self.logs, kind=self.kind)

    def configure(self) -> AwgServerConfig:
        shell = self._awg.shell
        config_file = {"file": self.kind.config_name}
        if self._awg.has_file(self.kind.config_path):
            self._ctx.log(_("%(file)s already exists and is kept.") % config_file)
        elif self._files:
            for path, content in self._files.by_path().items():
                shell.write_file_atomic(path, content, mode=0o600)
            self._ctx.log(_("%(file)s is restored with the saved server keys.") % config_file)
        else:
            shell.run(self.script("configure_container.sh"))
            self._ctx.log(_("%(file)s is created with new server keys.") % config_file)
        if not self._awg.has_file(self.kind.clients_table_path):
            # The Amnezia app cannot list clients of a server without this file.
            shell.write_file_atomic(
                self.kind.clients_table_path, ClientsTable([]).serialize().encode()
            )
        return AwgServerConfig.parse(shell.read_file(self.kind.config_path).decode())

    def start(self, result: AwgServerConfig) -> None:
        self._awg.start_awg(result.address.network)
        public_key = KeyPair.from_private_key(result.private_key or "").public_key
        self._ctx.log(
            _("%(title)s listens on UDP %(port)s, clients get addresses from %(subnet)s.")
            % {"title": self.title, "port": result.listen_port, "subnet": result.address.network}
        )
        self._ctx.log(_("Server public key: %(key)s") % {"key": public_key})


@installers.register
class AwgInstaller(WgInstaller):
    """amnezia-awg2 with awg0.conf; the kernel must be new enough for AmneziaWG 2.0."""

    kind = AWG2
    container = AWG2.name
    folder = SCRIPT_FOLDERS[AWG2.name]
    title = CATALOG[AWG2.name].title
    default_port = DEFAULT_PORT
    low_port = True

    def check_server(self) -> None:
        with self._ctx.step(_("Check the kernel version")):
            release = self._host.run("uname -r").strip()
            match = _KERNEL_VERSION.match(release)
            if match and (int(match[1]), int(match[2])) < _MIN_KERNEL:
                raise SetupError(SetupErrorCode.KERNEL_TOO_OLD, release)


@installers.register
class AwgLegacyInstaller(WgInstaller):
    """amnezia-awg with wg0.conf, as the Amnezia app installs AmneziaWG legacy."""

    kind = AWG_LEGACY
    container = AWG_LEGACY.name
    folder = SCRIPT_FOLDERS[AWG_LEGACY.name]
    title = CATALOG[AWG_LEGACY.name].title
    default_port = DEFAULT_PORT
    low_port = True


@installers.register
class WireGuardInstaller(WgInstaller):
    """amnezia-wireguard with wg0.conf and no obfuscation."""

    kind = WIREGUARD
    container = WIREGUARD.name
    folder = SCRIPT_FOLDERS[WIREGUARD.name]
    title = CATALOG[WIREGUARD.name].title
    default_port = WIREGUARD_DEFAULT_PORT
