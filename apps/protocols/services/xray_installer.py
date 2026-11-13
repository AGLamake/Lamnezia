"""Installing XRay (VLESS over TCP with Reality) with the upstream server scripts.

Follows upstream server_scripts/xray/ and XrayConfigurator::createConfig: configure_container.sh
creates the client UUID of the installer, the shortId and the Reality key pair in the data
volume, the panel writes server.json as XrayServerConfig::toServerInboundJson does.
"""

from collections.abc import Mapping
from typing import Any, Self

from django.utils.translation import gettext as _

from amnezia_core.clients_table import ClientsTable
from amnezia_core.errors import ConfigFormatError, UnsupportedSetupError
from amnezia_core.script_vars import container_variables
from amnezia_core.xray import (
    DEFAULT_PORT,
    DEFAULT_SITE,
    XRAY_CLIENTS_TABLE_PATH,
    XRAY_CONFIG_PATH,
    XRAY_CONTAINER,
    XRAY_DIR,
    XRAY_OWN_CLIENT_PATH,
    XRAY_PRIVATE_KEY_PATH,
    XRAY_PUBLIC_KEY_PATH,
    XRAY_SHORT_ID_PATH,
    XrayServerConfig,
    XraySettings,
    is_valid_site,
)
from apps.operations.services.context import OperationContext
from apps.protocols.catalog import CATALOG
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.container import Transport, log_arguments
from apps.protocols.services.installer import START_SCRIPT, ContainerInstaller, installers
from apps.protocols.services.xray_container import XRAY_FOLDER, XrayDocker
from apps.ssh.errors import RemoteFileNotFoundError
from apps.ssh.services import RemoteHost

_FILE_MODE = 0o600
_KEYGEN_FAILED = "amnezia_xray_keygen=failed"
# run_container.sh creates the tun device after `docker run`.
_TUN_SCRIPT = "mkdir -p /dev/net; if [ ! -c /dev/net/tun ]; then mknod /dev/net/tun c 10 200; fi"
_STARTS_XRAY = "xray -config"


@installers.register
class XrayInstaller(ContainerInstaller[XrayServerConfig]):
    """amnezia-xray: one VLESS inbound on TCP `port`, masked as `site` (the app's default site).

    Keys and server.json already in the data volume are kept, so a retry or a server set up in
    the Amnezia app keeps its clients. The secrets are never written to the step log.
    """

    container = XRAY_CONTAINER
    title = CATALOG[XRAY_CONTAINER].title
    folder = XRAY_FOLDER
    transport = Transport.TCP
    default_port = DEFAULT_PORT
    data_volume = True
    install_fields = ("site",)

    def __init__(
        self,
        host: RemoteHost,
        ctx: OperationContext,
        *,
        port: int,
        site: str = DEFAULT_SITE,
        logs: bool = False,
    ) -> None:
        if not is_valid_site(site):
            raise ValueError(f"invalid masking site {site!r}")
        self._xray = XrayDocker(host, ctx)
        super().__init__(host, ctx, self._xray, port=port, logs=logs)
        self.site = site.strip()

    @classmethod
    def published_ports(cls, port: int) -> set[tuple[int, str]]:
        """run_container.sh publishes the port on TCP and UDP."""
        return {(port, str(Transport.TCP)), (port, str(Transport.UDP))}

    @classmethod
    def from_params(
        cls, host: RemoteHost, ctx: OperationContext, params: Mapping[str, Any]
    ) -> Self:
        site = str(params.get("site") or DEFAULT_SITE)
        return cls(host, ctx, port=int(params["port"]), site=site, logs=bool(params["logs"]))

    def script_variables(self) -> dict[str, str]:
        return {
            **container_variables(self.container),
            "$XRAY_SERVER_PORT": str(self.port),
            "$XRAY_SITE_NAME": self.site,
        }

    def run_arguments(self) -> list[str]:
        port = self.port
        return [
            "docker", "run", "-d",
            "--privileged",
            *log_arguments(logs=self.logs),
            "--restart", "always",
            "--cap-add=NET_ADMIN",
            "-p", f"{port}:{port}/tcp",
            "-p", f"{port}:{port}/udp",
            "-v", f"{self.container}-data:{XRAY_DIR}",
            "--name", self.container,
            self.container,
        ]  # fmt: skip

    def configure(self) -> XrayServerConfig:
        shell = self._xray.shell
        shell.run(_TUN_SCRIPT)
        if _KEYGEN_FAILED in shell.run(self.script("configure_container.sh")):
            raise SetupError(SetupErrorCode.XRAY_KEYGEN_FAILED)
        config = self._existing_config()
        if config is None:
            settings = XraySettings(port=self.port, site=self.site, sni=self.site)
            config = XrayServerConfig.for_setup(
                settings,
                own_client_id=self._key(XRAY_OWN_CLIENT_PATH),
                private_key=self._key(XRAY_PRIVATE_KEY_PATH),
                short_id=self._key(XRAY_SHORT_ID_PATH),
            )
            shell.write_file_atomic(XRAY_CONFIG_PATH, config.serialize().encode(), _FILE_MODE)
            self._ctx.log(_("server.json is created with new Reality keys."))
        if not self._xray.has_file(XRAY_CLIENTS_TABLE_PATH):
            shell.write_file_atomic(
                XRAY_CLIENTS_TABLE_PATH, ClientsTable([]).serialize().encode(), _FILE_MODE
            )
        return config

    def start(self, result: XrayServerConfig) -> None:
        """Write the real start.sh over the placeholder of the image and run it."""
        shell = self._xray.shell
        if _STARTS_XRAY not in shell.read_file(START_SCRIPT).decode():
            shell.write_file_atomic(START_SCRIPT, self.script("start.sh").encode(), mode=0o755)
        if self._xray.xray_is_up():
            self._ctx.log(_("XRay is already running."))
        else:
            command = f"chmod a+x {START_SCRIPT} && {START_SCRIPT}"
            self._host.docker("exec", "-d", self.container, "sh", "-c", command)
            self._xray.wait_for_xray()
        settings = result.settings()
        self._ctx.log(
            _("XRay listens on TCP %(port)s and is masked as %(site)s.")
            % {"port": settings.port, "site": settings.site}
        )
        self._ctx.log(_("Reality public key: %(key)s") % {"key": self._key(XRAY_PUBLIC_KEY_PATH)})

    def _existing_config(self) -> XrayServerConfig | None:
        """server.json already in the volume; an empty file counts as none, as upstream."""
        try:
            text = self._xray.shell.read_file(XRAY_CONFIG_PATH).decode()
        except RemoteFileNotFoundError:
            return None
        if not text.strip():
            return None
        try:
            config = XrayServerConfig.parse(text)
            config.settings()
        except (ConfigFormatError, UnsupportedSetupError) as error:
            raise SetupError(SetupErrorCode.XRAY_CONFIG_INVALID, str(error)) from error
        self._ctx.log(_("%(file)s already exists and is kept.") % {"file": "server.json"})
        return config

    def _key(self, path: str) -> str:
        try:
            value = self._xray.shell.read_file(path).decode().strip()
        except RemoteFileNotFoundError:
            value = ""
        if not value:
            raise SetupError(SetupErrorCode.XRAY_KEYGEN_FAILED, path)
        return value
