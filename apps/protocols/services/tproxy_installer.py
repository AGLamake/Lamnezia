"""Installing TProxy, the WEB proxy of Telegram of the Amnezia app, behind Caddy on its own domain.

Follows upstream server_scripts/tproxy/ and genTProxyVars. Caddy gets a certificate from
Let's Encrypt over port 80, so before anything changes the installer checks what the app leaves
to the administrator: the domain points at the server and port 80 is free.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self

from django.utils.translation import gettext as _

from amnezia_core.openvpn import is_ipv4
from amnezia_core.proxies import (
    TPROXY_CONTAINER,
    TPROXY_HTTP_PORT,
    TPROXY_META_PATH,
    TPROXY_PORT,
    parse_meta,
)
from apps.operations.services.context import OperationContext
from apps.protocols.catalog import CATALOG
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.container import Transport
from apps.protocols.services.installer import installers, ipv4_addresses, resolve_server_ip
from apps.protocols.services.proxy_installers import TelegramProxyInstaller
from apps.ssh.services import RemoteHost


@dataclass(frozen=True)
class TProxySite:
    """Where TProxy answers: its domain, the e-mail Caddy gives Let's Encrypt for the
    certificate and the address of the server the panel connects to.
    """

    hostname: str
    email: str
    server_host: str = ""


@installers.register
class TProxyInstaller(TelegramProxyInstaller):
    """amnezia-tproxy: the WEB proxy of Telegram behind Caddy, on HTTPS of its own domain.

    start.sh gets the IP of the server, as in the app, for the NAT of the MTProto proxy inside.
    """

    container = TPROXY_CONTAINER
    title = CATALOG[TPROXY_CONTAINER].title
    folder = "tproxy"
    default_port = TPROXY_PORT
    secret_variable = "TPROXY"
    install_fields = ("domain", "email")

    def __init__(
        self,
        host: RemoteHost,
        ctx: OperationContext,
        *,
        port: int,
        site: TProxySite,
        logs: bool = False,
    ) -> None:
        super().__init__(host, ctx, port=port, logs=logs)
        self.site = site
        self.server_ip = site.server_host if is_ipv4(site.server_host) else ""

    @classmethod
    def from_params(
        cls, host: RemoteHost, ctx: OperationContext, params: Mapping[str, Any]
    ) -> Self:
        return cls(
            host,
            ctx,
            port=int(params["port"]),
            site=TProxySite(
                hostname=str(params["hostname"]),
                email=str(params["email"]),
                server_host=str(params.get("server_host", "")),
            ),
            logs=bool(params["logs"]),
        )

    @classmethod
    def from_container(cls, host: RemoteHost, ctx: OperationContext, port: int) -> Self:
        meta = parse_meta(cls._container_file(host, TPROXY_META_PATH))
        https_port = meta.get("https_port", "")
        site = TProxySite(hostname=meta.get("hostname", ""), email=meta.get("email", ""))
        return cls(host, ctx, port=int(https_port) if https_port.isdigit() else port, site=site)

    @classmethod
    def published_ports(cls, port: int) -> set[tuple[int, str]]:
        return {(port, str(Transport.TCP)), (TPROXY_HTTP_PORT, str(Transport.TCP))}

    def check_server(self) -> None:
        """Refuse a domain that points elsewhere and a busy port 80: Caddy would get no
        certificate, and the panel knows only the ports of the Amnezia containers.
        """
        if not self.server_ip:
            self.server_ip = resolve_server_ip(self._ctx, self.site.server_host)
        with self._ctx.step(_("Check the domain of the proxy")):
            self._check_domain()
            if not self._docker.state():
                self._docker.check_port(
                    TPROXY_HTTP_PORT, Transport.TCP, SetupErrorCode.TPROXY_HTTP_PORT_BUSY
                )

    def _check_domain(self) -> None:
        hostname = self.site.hostname
        try:
            addresses = ipv4_addresses(hostname)
        except OSError as error:
            raise SetupError(SetupErrorCode.TPROXY_DOMAIN_UNRESOLVED, hostname) from error
        self._ctx.log(_("%(host)s is %(ip)s.") % {"host": hostname, "ip": ", ".join(addresses)})
        if self.server_ip not in addresses:
            detail = f"{hostname} → {', '.join(addresses)}, {self.server_ip}"
            raise SetupError(SetupErrorCode.TPROXY_DOMAIN_ELSEWHERE, detail)

    def run_arguments(self) -> list[str]:
        return self._run_proxy(
            "-p", f"{TPROXY_HTTP_PORT}:80/tcp", "-p", f"{self.port}:443/tcp"
        )  # fmt: skip

    def script_variables(self) -> dict[str, str]:
        return {
            **super().script_variables(),
            "$TPROXY_HOSTNAME": self.site.hostname,
            "$TPROXY_ACME_EMAIL": self.site.email,
            "$TPROXY_HTTPS_PORT": str(self.port),
            "$TPROXY_HTTP_PORT": str(TPROXY_HTTP_PORT),
            "$TPROXY_CARRIER_MODE": "https",
            "$TPROXY_WORKERS": "1",
            "$SERVER_IP_ADDRESS": self.server_ip,
        }
