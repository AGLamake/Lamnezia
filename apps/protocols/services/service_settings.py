"""Changing the port and the settings of the proxies: SOCKS5, MTProxy, Telemt and TProxy.

The Amnezia app saves such a change by running configure_container.sh again (the Telegram
proxies) or by reinstalling the container (SOCKS5). The panel runs the script with the new
settings and restarts the proxy; if that fails, the old settings files come back. Docker cannot
change a published port, so a new port gets a new container, and the old one waits aside until
it runs. The secret of a Telegram proxy stays in its
data volume, so its links change only with the port, the domain or the mode.
"""

from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any

from django.utils.translation import gettext as _

from amnezia_core.docker_ps import RunningContainer
from amnezia_core.proxies import SOCKS5_CONTAINER, TPROXY_CONTAINER, Socks5Account
from amnezia_core.proxy_options import TelegramProxyOptions
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.models import InstanceOrigin, ServiceInstance
from apps.protocols.services.container import ProtocolContainer, Transport
from apps.protocols.services.container_swap import ContainerSwap
from apps.protocols.services.installer import START_SCRIPT, installers, resolve_server_ip
from apps.protocols.services.proxy_installers import (
    MtProtoInstaller,
    ProxyInstaller,
    Socks5Installer,
    TelemtInstaller,
)
from apps.protocols.services.service_import import ServiceFiles
from apps.protocols.services.settings_backup import SettingsBackup
from apps.protocols.services.tproxy_installer import TProxyInstaller, TProxySite
from apps.protocols.signals import service_changed
from apps.ssh.encryption import SecretCipher
from apps.ssh.services import RemoteHost

SERVICE_SETTINGS_KIND = "protocols.change_service_settings"


@dataclass(frozen=True)
class ServiceSettings:
    """The new port and settings of a proxy, as its settings page gives them.

    `options` are the MTProxy and Telemt settings of TelegramProxyOptions the page knew, the
    `hostname` and `email` of TProxy or the `user` of SOCKS5; what the page did not know stays as
    the container has it. The SOCKS5 password travels in the operation encrypted.
    """

    port: int
    options: dict[str, Any] = field(default_factory=dict)
    password: str = ""

    def __repr__(self) -> str:
        return f"ServiceSettings(port={self.port!r}, options={self.options!r})"

    @classmethod
    def from_params(cls, data: Mapping[str, Any]) -> "ServiceSettings":
        token = data.get("password_token") or ""
        password = SecretCipher.from_settings().decrypt(token) if token else ""
        return cls(int(data["port"]), dict(data.get("options") or {}), password)

    def to_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"port": self.port, "options": self.options}
        if self.password:
            params["password_token"] = SecretCipher.from_settings().encrypt(self.password)
        return params

    def installers(
        self, host: RemoteHost, ctx: OperationContext, service: ServiceInstance
    ) -> tuple[ProxyInstaller, ProxyInstaller]:
        """The installers with the settings of the container and with the new ones."""
        container = service.container
        port = service.port or 0
        if container == SOCKS5_CONTAINER:
            account = Socks5Account(self.options["user"], self.password)
            return (
                Socks5Installer(host, ctx, port=port),
                Socks5Installer(host, ctx, port=self.port, account=account),
            )
        if container == TPROXY_CONTAINER:
            return self._tproxy(host, ctx, service)
        installer = installers.get(container)
        assert issubclass(installer, MtProtoInstaller), container
        current = installer.from_container(host, ctx, port)
        options = {**current.options.to_params(), **self.options}
        new = installer(
            host, ctx, port=self.port, options=TelegramProxyOptions.from_params(options)
        )
        if isinstance(current, TelemtInstaller) and isinstance(new, TelemtInstaller):
            new.switches = current.switches
        return current, new

    def _tproxy(
        self, host: RemoteHost, ctx: OperationContext, service: ServiceInstance
    ) -> tuple[ProxyInstaller, ProxyInstaller]:
        current = TProxyInstaller.from_container(host, ctx, service.port or 0)
        site = TProxySite(
            hostname=self.options.get("hostname") or current.site.hostname,
            email=self.options.get("email") or current.site.email,
            server_host=service.server.host,
        )
        new = TProxyInstaller(host, ctx, port=self.port, site=site)
        new.server_ip = current.server_ip = new.server_ip or resolve_server_ip(
            ctx, site.server_host
        )
        return current, new


def start_service_settings_change(
    service: ServiceInstance, settings: ServiceSettings, user: User, *, ip: str | None = None
) -> Operation:
    params = {"service_id": service.pk, **settings.to_params()}
    operation = launch(SERVICE_SETTINGS_KIND, params, user=user, subject=str(service))
    data = {"container": service.container, "port": settings.port, "options": settings.options}
    audit.log(SERVICE_SETTINGS_KIND, actor=user, target=service.server, data=data, ip=ip)
    return operation


class ServiceReconfiguration:
    """Applies the settings of a proxy under the server lock (DESIGN.md 5.4)."""

    def __init__(self, host: RemoteHost, service: ServiceInstance, ctx: OperationContext) -> None:
        self._host = host
        self._service = service
        self._ctx = ctx
        self._docker = ProtocolContainer(host, ctx, service.container)

    def apply(self, settings: ServiceSettings) -> None:
        service = self._service
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            if self._docker.state() != "running":
                raise SetupError(SetupErrorCode.PROXY_NOT_RUNNING, service.container)
            with self._ctx.step(_("Read the server config")):
                current, new = settings.installers(self._host, self._ctx, service)
            new.check_server()
            if new.port != current.port:
                self._move(current, new)
            else:
                self._reconfigure(new)
            with self._ctx.step(_("Read the settings back")):
                running = RunningContainer(service.container, new.port, str(new.transport))
                origin = InstanceOrigin(service.origin)
                saved = ServiceFiles.read(self._host, running).save(service.server, origin)
        service_changed.send(type(self), service=saved)

    def _reconfigure(self, installer: ProxyInstaller) -> None:
        """configure_container.sh and start.sh with the new settings, then a restart.

        If any of it fails, the files of the old settings come back and the proxy restarts
        with them.
        """
        title = {"title": installer.title}
        backup = SettingsBackup(
            self._docker.shell, self._ctx, installer.settings_folders, [START_SCRIPT]
        )
        with backup.kept(restart=lambda: self._host.docker("restart", installer.container)):
            with self._ctx.step(_("Configure %(title)s") % title):
                installer.configure()
                installer.write_start_script()
            with self._ctx.step(_("Restart %(title)s") % title):
                self._host.docker("restart", installer.container)

    def _move(self, current: ProxyInstaller, new: ProxyInstaller) -> None:
        """A new container on the new port; a shared volume gets the old settings back on
        failure, and the old container rereads them.
        """
        added = new.published_ports(new.port) - current.published_ports(current.port)
        ports = [(port, Transport(transport)) for port, transport in added]

        def restore() -> None:
            current.configure()
            self._host.docker("restart", current.container)

        swap = ContainerSwap(self._host, self._ctx, self._docker)
        with swap.replace(ports, restore=restore if new.data_volume else None):
            self._docker.run_container(new.run_arguments())
            new.configure()
            new.start(None)
