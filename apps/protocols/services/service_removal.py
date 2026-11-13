"""Removal of a service container, as upstream server_scripts/remove_container.sh does it.

A service has no keys and no backup: the panel forgets it with the container. Files uploaded to
SFTP, the Tor site and the data volume of a Telegram proxy go away with it.
"""

from contextlib import ExitStack

from django.db import transaction
from django.utils.translation import gettext as _

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.models import ServiceInstance
from apps.protocols.services.container import ProtocolContainer
from apps.protocols.services.installer import installers
from apps.ssh.services import RemoteHost

REMOVE_SERVICE_KIND = "protocols.remove_service"


def start_service_removal(
    service: ServiceInstance, user: User, *, ip: str | None = None
) -> Operation:
    operation = launch(
        REMOVE_SERVICE_KIND, {"service_id": service.pk}, user=user, subject=str(service)
    )
    data = {"container": service.container}
    audit.log(REMOVE_SERVICE_KIND, actor=user, target=service.server, data=data, ip=ip)
    return operation


class ServiceRemoval:
    """Removes the container and its image under the server lock, then forgets the service."""

    def __init__(self, host: RemoteHost, service: ServiceInstance, ctx: OperationContext) -> None:
        self._host = host
        self._service = service
        self._ctx = ctx
        self._docker = ProtocolContainer(host, ctx, service.container)

    def run(self) -> None:
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Remove the container")):
                if self._docker.state():
                    self._docker.remove_with_image(data_volume=self._has_data_volume())
                else:
                    self._ctx.log(_("The server has no such container any more."))
        self._forget()

    def _has_data_volume(self) -> bool:
        """The Telegram proxies keep their secret in a volume that goes with them."""
        installer = installers.find(self._service.container)
        return bool(installer and installer.data_volume)

    @transaction.atomic
    def _forget(self) -> None:
        service = self._service
        server = service.server
        server.record_containers([c for c in server.containers if c["name"] != service.container])
        service.delete()
