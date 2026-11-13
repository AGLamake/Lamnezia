"""Installing the proxies of the Amnezia app: SOCKS5, MTProxy and Telemt.

Follows upstream server_scripts/{socks5_proxy,mtproxy,telemt}/ and genSocks5ProxyVars,
genMtProxyVars, genTelemtVars: the image is built from the Dockerfile of the app,
configure_container.sh writes the settings into the container and start.sh starts the proxy.
MTProxy and Telemt get the settings of the install form (TelegramProxyOptions), saved where the
app reads them back; TProxy is in tproxy_installer.py. The import that follows reads the secret
and the password back, as the app does; neither is written to the step log.
"""

import posixpath
from abc import abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar, Self

from django.utils.translation import gettext as _

from amnezia_core.errors import ConfigFormatError
from amnezia_core.proxies import (
    MTPROXY_CONTAINER,
    MTPROXY_META_PATH,
    MTPROXY_PORT,
    SOCKS5_CONFIG_PATH,
    SOCKS5_CONTAINER,
    SOCKS5_PORT,
    TELEMT_CONFIG_PATH,
    TELEMT_CONTAINER,
    TELEMT_PORT,
    TELEMT_USER,
    Socks5Account,
    TelemtConfig,
    TelemtSwitches,
    configure_secret,
    parse_meta,
)
from amnezia_core.proxy_options import TelegramProxyOptions
from apps.operations.services.context import OperationContext
from apps.protocols.catalog import CATALOG
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.container import Transport
from apps.protocols.services.installer import installers
from apps.protocols.services.service_import import read_text
from apps.protocols.services.service_installers import ServiceInstaller
from apps.ssh.errors import RemoteFileNotFoundError
from apps.ssh.services import RemoteHost

# configure_container.sh of the Telegram proxies downloads the files of Telegram.
_CONFIGURE_TIMEOUT = 180


class ProxyInstaller(ServiceInstaller):
    """A proxy container: configured by its configure_container.sh, started by its start.sh."""

    transport = Transport.TCP
    has_start_script = True
    joins_network = False
    # Where configure_container.sh writes the settings; kept aside while they change.
    settings_folders: ClassVar[tuple[str, ...]] = ("/data",)

    def _run_proxy(self, *options: str) -> list[str]:
        """`docker run` with `options` and the data volume, if the proxy has one."""
        volume = ("-v", f"{self.container}-data:/data") if self.data_volume else ()
        return self._run(*options, *volume)


class TelegramProxyInstaller(ProxyInstaller):
    """MTProxy, Telemt and TProxy keep their secret in the `<container>-data` volume.

    The panel passes no secret, so configure_container.sh keeps the secret of the volume and
    makes one only for a new volume: a retry does not change the links already given out.
    `renew_secret` asks the script for a new one, as `*_REGENERATE_SECRET` of the app does.
    """

    conntrack = True
    data_volume = True
    secret_variable: ClassVar[str]
    regenerate_secret = False

    @classmethod
    @abstractmethod
    def from_container(cls, host: RemoteHost, ctx: OperationContext, port: int) -> Self:
        """The installer with the settings the running container keeps in its files."""

    def renew_secret(self) -> None:
        """Write the settings again with a new secret and restart the proxy: old links stop."""
        self.regenerate_secret = True
        with self._ctx.step(_("Configure %(title)s") % {"title": self.title}):
            self.configure()
        with self._ctx.step(_("Restart %(title)s") % {"title": self.title}):
            # start.sh is the entry point of the image and reads the secret from the volume.
            self._host.docker("restart", self.container)

    def script_variables(self) -> dict[str, str]:
        prefix = self.secret_variable
        return {
            **super().script_variables(),
            f"${prefix}_SECRET": "",
            f"${prefix}_REGENERATE_SECRET": "1" if self.regenerate_secret else "0",
        }

    def configure(self) -> None:
        """Run configure_container.sh; its output has the secret, so it is not logged."""
        script = self.script("configure_container.sh")
        output = self._docker.shell.sh(script, timeout=_CONFIGURE_TIMEOUT).output
        try:
            configure_secret(output)
        except ConfigFormatError as error:
            raise SetupError(SetupErrorCode.PROXY_CONFIGURE_FAILED, self.container) from error
        self._ctx.log(_("The secret of the proxy is ready."))

    @classmethod
    def _container_file(cls, host: RemoteHost, path: str) -> str:
        return read_text(host.container(cls.container), path)


class MtProtoInstaller(TelegramProxyInstaller):
    """MTProxy and Telemt: MTProto proxies with the settings the app lets one choose."""

    install_fields: ClassVar[tuple[str, ...]] = (
        "mode", "tls_domain", "public_host", "tag", "additional_secrets", "nat_external_ip",
    )  # fmt: skip

    def __init__(
        self,
        host: RemoteHost,
        ctx: OperationContext,
        *,
        port: int,
        logs: bool = False,
        options: TelegramProxyOptions | None = None,
    ) -> None:
        super().__init__(host, ctx, port=port, logs=logs)
        self.options = options or TelegramProxyOptions()

    @classmethod
    def from_params(
        cls, host: RemoteHost, ctx: OperationContext, params: Mapping[str, Any]
    ) -> Self:
        options = TelegramProxyOptions.from_params(params.get("proxy") or {})
        return cls(host, ctx, port=int(params["port"]), logs=bool(params["logs"]), options=options)

    def run_arguments(self) -> list[str]:
        return self._run_proxy("-p", f"{self.port}:{self.port}/tcp")


@installers.register
class Socks5Installer(ProxyInstaller):
    """amnezia-socks5proxy: 3proxy with one account, `proxy_user` and a new random password.

    A container already on the server keeps the account of its 3proxy.cfg, unless the
    settings page gives a new one (`keeps_account`).
    """

    container = SOCKS5_CONTAINER
    title = CATALOG[SOCKS5_CONTAINER].title
    folder = "socks5_proxy"
    default_port = SOCKS5_PORT
    settings_folders = (posixpath.dirname(SOCKS5_CONFIG_PATH),)

    def __init__(
        self,
        host: RemoteHost,
        ctx: OperationContext,
        *,
        port: int,
        logs: bool = False,
        account: Socks5Account | None = None,
    ) -> None:
        super().__init__(host, ctx, port=port, logs=logs)
        self.account = account or Socks5Account.generate()
        self.keeps_account = account is None

    @classmethod
    def from_params(
        cls, host: RemoteHost, ctx: OperationContext, params: Mapping[str, Any]
    ) -> Self:
        return cls(host, ctx, port=int(params["port"]), logs=bool(params["logs"]))

    def run_arguments(self) -> list[str]:
        return self._run_proxy("-p", f"{self.port}:{self.port}/tcp")

    def script_variables(self) -> dict[str, str]:
        return {
            **super().script_variables(),
            "$SOCKS5_PROXY_PORT": str(self.port),
            "$SOCKS5_USER": self.account.users_line,
            "$SOCKS5_AUTH_TYPE": "strong",
        }

    def configure(self) -> None:
        """Write 3proxy.cfg; the script has the password, so neither is logged."""
        try:
            current = self._docker.shell.read_file(SOCKS5_CONFIG_PATH).decode()
        except RemoteFileNotFoundError:
            current = ""
        kept = Socks5Account.from_config(current) if self.keeps_account else None
        if kept:
            self.account = kept
            self._ctx.log(_("The account of the proxy is kept."))
        result = self._docker.shell.sh(self.script("configure_container.sh"))
        if not result.ok:
            raise SetupError(SetupErrorCode.PROXY_CONFIGURE_FAILED, self.container)


@installers.register
class MtProxyInstaller(MtProtoInstaller):
    """amnezia-mtproxy: the official MTProxy of Telegram; start.sh gets the settings too."""

    container = MTPROXY_CONTAINER
    title = CATALOG[MTPROXY_CONTAINER].title
    folder = "mtproxy"
    default_port = MTPROXY_PORT
    secret_variable = "MTPROXY"
    install_fields = (*MtProtoInstaller.install_fields, "nat_internal_ip")

    @classmethod
    def from_container(cls, host: RemoteHost, ctx: OperationContext, port: int) -> Self:
        meta = parse_meta(cls._container_file(host, MTPROXY_META_PATH))
        return cls(host, ctx, port=port, options=TelegramProxyOptions.from_mtproxy_meta(meta))

    def script_variables(self) -> dict[str, str]:
        return {
            **super().script_variables(),
            "$MTPROXY_PORT": str(self.port),
            **self.options.mtproxy_variables(),
        }


@installers.register
class TelemtInstaller(MtProtoInstaller):
    """amnezia-telemt: Telemt, an MTProto proxy, with the user `amnezia`.

    `switches` are the TOML booleans the app does not show; those of a configured container
    are kept when its secret is renewed.
    """

    container = TELEMT_CONTAINER
    title = CATALOG[TELEMT_CONTAINER].title
    folder = "telemt"
    default_port = TELEMT_PORT
    secret_variable = "TELEMT"
    switches = TelemtSwitches()

    @classmethod
    def from_container(cls, host: RemoteHost, ctx: OperationContext, port: int) -> Self:
        config = TelemtConfig.parse(cls._container_file(host, TELEMT_CONFIG_PATH))
        options = TelegramProxyOptions.from_telemt_config(config)
        installer = cls(host, ctx, port=config.port or port, options=options)
        installer.switches = config.switches
        return installer

    def run_arguments(self) -> list[str]:
        # Upstream: the limit of open files Telemt asks for under load.
        ulimit = ("--ulimit", "nofile=65536:65536")
        return self._run_proxy(*ulimit, "-p", f"{self.port}:{self.port}/tcp")

    def script_variables(self) -> dict[str, str]:
        return {
            **super().script_variables(),
            "$TELEMT_PORT": str(self.port),
            "$TELEMT_USER_NAME": TELEMT_USER,
            **self.options.telemt_variables(),
            **self.switches.variables(),
        }
