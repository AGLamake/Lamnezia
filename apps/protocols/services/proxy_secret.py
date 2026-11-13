"""A new secret of a Telegram proxy from the panel, as `*_REGENERATE_SECRET` of the Amnezia app.

configure_container.sh runs again with the settings the container keeps and a new secret; the
proxy restarts and the service is read back, so its links change at once and the old ones stop.
"""

from contextlib import ExitStack

from django.utils.translation import gettext as _

from amnezia_core.docker_ps import RunningContainer
from amnezia_core.proxies import TELEGRAM_PROXIES
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.models import InstanceOrigin, ServiceInstance
from apps.protocols.services.container import ProtocolContainer
from apps.protocols.services.installer import installers
from apps.protocols.services.proxy_installers import TelegramProxyInstaller
from apps.protocols.services.service_import import ServiceFiles
from apps.protocols.signals import service_changed
from apps.ssh.services import RemoteHost

RENEW_SECRET_KIND = "protocols.renew_proxy_secret"


def renews_secret(service: ServiceInstance) -> bool:
    """MTProxy, Telemt and TProxy have a secret the panel can renew."""
    return service.container in TELEGRAM_PROXIES


def start_secret_renewal(
    service: ServiceInstance, user: User, *, ip: str | None = None
) -> Operation:
    operation = launch(
        RENEW_SECRET_KIND, {"service_id": service.pk}, user=user, subject=str(service)
    )
    data = {"container": service.container}
    audit.log(RENEW_SECRET_KIND, actor=user, target=service.server, data=data, ip=ip)
    return operation


class SecretRenewal:
    """Renews the secret under the server lock and reads the service back with its new links."""

    def __init__(self, host: RemoteHost, service: ServiceInstance, ctx: OperationContext) -> None:
        self._host = host
        self._service = service
        self._ctx = ctx

    def run(self) -> None:
        service = self._service
        installer = installers.get(service.container)
        assert issubclass(installer, TelegramProxyInstaller), service.container
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            docker = ProtocolContainer(self._host, self._ctx, service.container)
            if docker.state() != "running":
                raise SetupError(SetupErrorCode.PROXY_NOT_RUNNING, service.container)
            proxy = installer.from_container(self._host, self._ctx, service.port or 0)
            proxy.renew_secret()
            with self._ctx.step(_("Read the new links")):
                running = RunningContainer(service.container, proxy.port, str(proxy.transport))
                origin = InstanceOrigin(service.origin)
                saved = ServiceFiles.read(self._host, running).save(service.server, origin)
        service_changed.send(type(self), service=saved)
