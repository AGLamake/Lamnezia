"""Installing the Amnezia services with the upstream server scripts: AmneziaDNS, SFTP, Tor site.

Follows upstream server_scripts/{dns,sftp,website_tor}/: the image is built from the Dockerfile
of the app and started as its run_container.sh does. The import that follows the installation
reads the SFTP account and the onion address from the container, as the app does.
"""

from collections.abc import Mapping
from typing import Any, ClassVar, Self

from django.utils.translation import gettext as _

from amnezia_core.errors import ConfigFormatError
from amnezia_core.services import (
    DNS_ADDRESS,
    DNS_CONTAINER,
    DNS_PORT,
    SFTP_CONTAINER,
    SFTP_PORT,
    TOR_CONTAINER,
    SftpAccount,
    parse_onion,
)
from apps.operations.services.context import OperationContext
from apps.protocols.catalog import CATALOG
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.container import Transport, log_arguments
from apps.protocols.services.host_scripts import AMNEZIA_NETWORK
from apps.protocols.services.installer import ContainerInstaller, installers
from apps.ssh.services import RemoteHost

# configure_container.sh waits 5 seconds for Tor before it reads the address.
_TOR_CONFIGURE_TIMEOUT = 60


class ServiceInstaller(ContainerInstaller[None]):
    """A service container: nothing to configure and no start.sh, unlike the VPN protocols."""

    # The proxies start with upstream start.sh, the other services with `docker run`.
    has_start_script: ClassVar[bool] = False

    def configure(self) -> None:
        """The services of the app have an empty configure_container.sh."""

    def start(self, result: None) -> None:
        """`docker run` already starts the service, unless it has a start.sh."""
        if self.has_start_script:
            super().start(result)

    def _run(self, *options: str) -> list[str]:
        """`docker run` of the service with the `options` of its run_container.sh."""
        return [
            "docker", "run", "-d",
            *log_arguments(logs=self.logs),
            "--restart", "always",
            *options,
            "--name", self.container,
            self.container,
        ]  # fmt: skip


@installers.register
class DnsInstaller(ServiceInstaller):
    """amnezia-dns: Unbound with DNS over TLS, at a fixed address of amnezia-dns-net.

    VPN clients reach it through their container; nothing is published on the server.
    """

    container = DNS_CONTAINER
    title = CATALOG[DNS_CONTAINER].title
    folder = "dns"
    transport = Transport.UDP
    default_port = DNS_PORT
    publishes_port = False

    def run_arguments(self) -> list[str]:
        return self._run("--network", AMNEZIA_NETWORK, f"--ip={DNS_ADDRESS}")


@installers.register
class SftpInstaller(ServiceInstaller):
    """amnezia-sftp: one account with a new random password and an `upload` folder.

    A container already on the server keeps its account. The password is never written to the
    step log.
    """

    container = SFTP_CONTAINER
    title = CATALOG[SFTP_CONTAINER].title
    folder = "sftp"
    transport = Transport.TCP
    default_port = SFTP_PORT
    joins_network = False

    def __init__(
        self,
        host: RemoteHost,
        ctx: OperationContext,
        *,
        port: int,
        logs: bool = False,
        account: SftpAccount | None = None,
    ) -> None:
        super().__init__(host, ctx, port=port, logs=logs)
        self.account = account or SftpAccount.generate()

    @classmethod
    def from_params(
        cls, host: RemoteHost, ctx: OperationContext, params: Mapping[str, Any]
    ) -> Self:
        return cls(host, ctx, port=int(params["port"]), logs=bool(params["logs"]))

    def run_arguments(self) -> list[str]:
        arguments = self._run("-p", f"{self.port}:22/tcp")
        return [*arguments, self.account.argument]


@installers.register
class TorSiteInstaller(ServiceInstaller):
    """amnezia-torwebsite: a WordPress site behind a Tor hidden service, reached by its address."""

    container = TOR_CONTAINER
    title = CATALOG[TOR_CONTAINER].title
    folder = "website_tor"
    transport = Transport.TCP
    default_port = 80
    publishes_port = False
    joins_network = False

    def run_arguments(self) -> list[str]:
        return self._run()

    def configure(self) -> None:
        """Wait for the onion address as configure_container.sh does; it is not logged."""
        script = self.script("configure_container.sh")
        output = self._docker.shell.sh(script, timeout=_TOR_CONFIGURE_TIMEOUT).output
        try:
            parse_onion(output)
        except ConfigFormatError as error:
            raise SetupError(SetupErrorCode.TOR_NO_ONION, TOR_CONTAINER) from error
        self._ctx.log(_("The onion address of the site is ready."))
