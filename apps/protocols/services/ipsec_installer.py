"""Installing IPsec (IKEv2) with the upstream server scripts.

Follows upstream server_scripts/ipsec/: run_container.sh publishes UDP 500 and 4500,
configure_container.sh writes the libreswan and xl2tpd configuration, starts them and makes the
CA and the server certificate in the NSS database; start.sh is empty. The container has no data
volume, as upstream: the CA lives and dies with the container.
"""

from collections.abc import Mapping
from typing import Any, Self

from django.utils.translation import gettext as _

from amnezia_core.clients_table import ClientsTable
from amnezia_core.ipsec import IPSEC_CLIENTS_TABLE_PATH, IPSEC_CONTAINER, IPSEC_PORTS
from amnezia_core.openvpn import is_ipv4
from apps.operations.services.context import OperationContext
from apps.protocols.catalog import CATALOG
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.container import Transport, log_arguments
from apps.protocols.services.installer import (
    ContainerInstaller,
    installers,
    resolve_server_ip,
)
from apps.protocols.services.ipsec_container import IPSEC_FOLDER, IpsecDocker, configure_script
from apps.ssh.services import RemoteHost

_FILE_MODE = 0o600


@installers.register
class IpsecInstaller(ContainerInstaller[None]):
    """amnezia-ipsec on UDP 500 and 4500, the ports of the app, which cannot be changed.

    `server_host` is the address of the server; its IP is the identity of the server
    certificate, which the clients check. A CA and ikev2.conf already in the container are kept,
    so a server set up in the Amnezia app keeps its clients.
    """

    container = IPSEC_CONTAINER
    title = CATALOG[IPSEC_CONTAINER].title
    folder = IPSEC_FOLDER
    transport = Transport.UDP
    default_port = IPSEC_PORTS[0]
    fixed_port = True

    def __init__(
        self,
        host: RemoteHost,
        ctx: OperationContext,
        *,
        server_host: str,
        logs: bool = False,
    ) -> None:
        self._ipsec = IpsecDocker(host, ctx)
        super().__init__(host, ctx, self._ipsec, port=self.default_port, logs=logs)
        self.server_host = server_host
        self.server_ip = server_host if is_ipv4(server_host) else ""

    @classmethod
    def from_params(
        cls, host: RemoteHost, ctx: OperationContext, params: Mapping[str, Any]
    ) -> Self:
        return cls(
            host, ctx, server_host=str(params.get("server_host", "")), logs=bool(params["logs"])
        )

    @classmethod
    def published_ports(cls, port: int) -> set[tuple[int, str]]:
        return {(fixed, str(cls.transport)) for fixed in IPSEC_PORTS}

    def check_server(self) -> None:
        """The IP of a server given by name is looked up as the app does it, before any change."""
        if not self.server_ip:
            self.server_ip = resolve_server_ip(self._ctx, self.server_host)

    def run_arguments(self) -> list[str]:
        published = [argument for port in IPSEC_PORTS for argument in ("-p", f"{port}:{port}/udp")]
        return [
            "docker", "run", "-d",
            "--privileged",
            *log_arguments(logs=self.logs),
            "--restart", "always",
            *published,
            "--name", self.container,
            self.container,
        ]  # fmt: skip

    def configure(self) -> None:
        if self._ipsec.is_configured():
            self._ctx.log(_("The certificate authority of the container is kept."))
            self._ipsec.ensure_running()
        else:
            self._ctx.log(_("Making the certificate authority."))
            self._ipsec.reset()
            result = self._ipsec.run_script(configure_script(self.server_ip, with_pki=True))
            if not self._ipsec.is_configured():
                raise SetupError(SetupErrorCode.IPSEC_SETUP_FAILED, f"exit {result.exit_code}")
        if not self._ipsec.has_file(IPSEC_CLIENTS_TABLE_PATH):
            self._ipsec.shell.write_file_atomic(
                IPSEC_CLIENTS_TABLE_PATH, ClientsTable([]).serialize().encode(), _FILE_MODE
            )

    def start(self, result: None) -> None:
        super().start(result)
        self._ipsec.wait_for_pluto()
        self._ctx.log(
            _("IPsec listens on UDP %(ports)s.")
            % {"ports": ", ".join(str(port) for port in IPSEC_PORTS)}
        )
