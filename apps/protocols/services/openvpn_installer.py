"""Installing OpenVPN with the upstream server scripts.

Follows upstream server_scripts/openvpn/: run_container.sh makes the easy-rsa CA, the server
certificate, ta.key and the first CRL in the container, configure_container.sh writes
server.conf, start.sh sets up the firewall and runs openvpn. The container has no data volume,
as upstream: the CA lives and dies with the container.
"""

from collections.abc import Mapping
from typing import Any, Self

from django.utils.translation import gettext as _

from amnezia_core.clients_table import ClientsTable
from amnezia_core.errors import ConfigFormatError
from amnezia_core.openvpn import (
    DEFAULT_PORT,
    OPENVPN_CLIENTS_TABLE_PATH,
    OPENVPN_CONFIG_PATH,
    OPENVPN_CONTAINER,
    OPENVPN_DIR,
    OPENVPN_REQUESTS_DIR,
    SERVER_CERT_NAME,
    OpenVpnSettings,
    is_ipv4,
)
from apps.operations.services.context import OperationContext
from apps.protocols.catalog import CATALOG
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.container import Transport, log_arguments
from apps.protocols.services.installer import (
    ContainerInstaller,
    installers,
    resolve_server_ip,
)
from apps.protocols.services.openvpn_container import OPENVPN_FOLDER, OpenVpnDocker
from apps.ssh.errors import RemoteFileNotFoundError
from apps.ssh.services import RemoteHost

_FILE_MODE = 0o600
# run_container.sh creates the tun device after `docker run`.
_TUN_SCRIPT = "mkdir -p /dev/net; if [ ! -c /dev/net/tun ]; then mknod /dev/net/tun c 10 200; fi"
# The last file the CA setup copies out of pki/: with it there, the CA may already sign clients.
_CA_READY_PATH = f"{OPENVPN_DIR}/ca.crt"
_CRL_PATH = f"{OPENVPN_DIR}/crl.pem"
# The easy-rsa part of run_container.sh; it stops at the first failure, so a retry starts over.
_PKI_SCRIPT = f"""\
set -e
export EASYRSA_BATCH=1
mkdir -p {OPENVPN_REQUESTS_DIR}
cd {OPENVPN_DIR}
easyrsa init-pki
easyrsa gen-dh
cp pki/dh.pem {OPENVPN_DIR}
easyrsa build-ca nopass
easyrsa gen-req {SERVER_CERT_NAME} nopass
easyrsa sign-req server {SERVER_CERT_NAME}
openvpn --genkey --secret ta.key
cp pki/issued/{SERVER_CERT_NAME}.crt pki/private/{SERVER_CERT_NAME}.key {OPENVPN_DIR}
cp pki/ca.crt {OPENVPN_DIR}
"""
_CRL_SCRIPT = (
    f"export EASYRSA_BATCH=1; cd {OPENVPN_DIR} && easyrsa gen-crl && cp pki/crl.pem crl.pem"
)
# Diffie-Hellman parameters take minutes on a small server.
_PKI_TIMEOUT = 1800


@installers.register
class OpenVpnInstaller(ContainerInstaller[OpenVpnSettings]):
    """amnezia-openvpn on UDP `port` with the cipher and the subnet of the app's defaults.

    `server_host` is the address of the server; run_container.sh and start.sh give its IP to
    the container so that packets do not leave it from another address behind NAT. A CA and
    server.conf already in the container are kept, so a server set up in the Amnezia app keeps
    its clients.
    """

    container = OPENVPN_CONTAINER
    title = CATALOG[OPENVPN_CONTAINER].title
    folder = OPENVPN_FOLDER
    transport = Transport.UDP
    default_port = DEFAULT_PORT

    def __init__(
        self,
        host: RemoteHost,
        ctx: OperationContext,
        *,
        port: int,
        server_host: str,
        logs: bool = False,
    ) -> None:
        self._openvpn = OpenVpnDocker(host, ctx)
        super().__init__(host, ctx, self._openvpn, port=port, logs=logs)
        self.settings = OpenVpnSettings(port=port)
        self.server_host = server_host
        self.server_ip = server_host if is_ipv4(server_host) else ""

    @classmethod
    def from_params(
        cls, host: RemoteHost, ctx: OperationContext, params: Mapping[str, Any]
    ) -> Self:
        return cls(
            host,
            ctx,
            port=int(params["port"]),
            server_host=str(params.get("server_host", "")),
            logs=bool(params["logs"]),
        )

    @classmethod
    def ports_of(cls, port: int, params: Mapping[str, Any]) -> set[tuple[int, str]]:
        """A container set up in the app or changed on the panel may serve TCP."""
        return {(port, str(params.get("transport") or cls.transport))}

    @classmethod
    def for_settings(
        cls,
        host: RemoteHost,
        ctx: OperationContext,
        settings: OpenVpnSettings,
        *,
        server_host: str,
        logs: bool,
    ) -> Self:
        """An installer writing server.conf of `settings` into a container that has none."""
        installer = cls(host, ctx, port=settings.port, server_host=server_host, logs=logs)
        installer.settings = settings
        return installer

    def check_server(self) -> None:
        """The IP of a server given by name is looked up as the app does it, before any change."""
        if not self.server_ip:
            self.server_ip = resolve_server_ip(self._ctx, self.server_host)

    def script_variables(self) -> dict[str, str]:
        return {
            **super().script_variables(),
            **self.settings.script_variables(),
            "$SERVER_IP_ADDRESS": self.server_ip,
        }

    def run_arguments(self) -> list[str]:
        port, transport = self.port, self.settings.transport
        return [
            "docker", "run", "-d",
            "--privileged",
            *log_arguments(logs=self.logs),
            "--restart", "always",
            "--cap-add=NET_ADMIN",
            "-p", f"{port}:{port}/{transport}",
            "--name", self.container,
            self.container,
        ]  # fmt: skip

    def configure(self) -> OpenVpnSettings:
        shell = self._openvpn.shell
        shell.run(_TUN_SCRIPT)
        shell.run(f"ifconfig eth0:0 {self.server_ip} netmask 255.255.255.255 up")
        if self._openvpn.has_file(_CA_READY_PATH):
            self._ctx.log(_("The certificate authority of the container is kept."))
        else:
            self._ctx.log(_("Making the certificate authority: this can take a few minutes."))
            result = shell.sh(_PKI_SCRIPT, timeout=_PKI_TIMEOUT)
            if not result.ok:
                raise SetupError(SetupErrorCode.OPENVPN_PKI_FAILED, f"exit {result.exit_code}")
        if not self._openvpn.has_file(_CRL_PATH) and not shell.sh(_CRL_SCRIPT).ok:
            raise SetupError(SetupErrorCode.OPENVPN_PKI_FAILED, "gen-crl")
        existing = self._existing_settings()
        if existing is None:
            shell.run(self.script("configure_container.sh"))
        else:
            self.settings = existing  # start.sh then opens the subnet the server has
        if not self._openvpn.has_file(OPENVPN_CLIENTS_TABLE_PATH):
            shell.write_file_atomic(
                OPENVPN_CLIENTS_TABLE_PATH, ClientsTable([]).serialize().encode(), _FILE_MODE
            )
        return self.settings

    def start(self, result: OpenVpnSettings) -> None:
        super().start(result)
        self._openvpn.wait_for_openvpn()
        self._ctx.log(
            _("OpenVPN listens on %(transport)s %(port)s.")
            % {"transport": result.transport.upper(), "port": result.port}
        )

    def _existing_settings(self) -> OpenVpnSettings | None:
        try:
            text = self._openvpn.shell.read_file(OPENVPN_CONFIG_PATH).decode()
        except RemoteFileNotFoundError:
            return None
        try:
            settings = OpenVpnSettings.from_server_config(text)
        except ConfigFormatError as error:
            raise SetupError(SetupErrorCode.OPENVPN_CONFIG_INVALID, str(error)) from error
        self._ctx.log(_("%(file)s already exists and is kept.") % {"file": "server.conf"})
        return settings
