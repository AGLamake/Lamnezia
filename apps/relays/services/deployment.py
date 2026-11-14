"""Installing, updating and removing the relay of a server, and the periodic sync of relays.

An update gives every group of the relay its service peer on the members first, then writes
the members in order to the agent. The relay server is locked only for its own container: a
member on the same server takes the same lock for its peers.
"""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.services.host_setup import HostSetup
from apps.relays.errors import RelayError, RelayErrorCode
from apps.relays.models import Relay
from apps.relays.services.config import RelayPlan
from apps.relays.services.container import RESERVED_PORTS, RelayContainer
from apps.relays.services.service_peers import ServicePeerPlacement
from apps.servers.models import Server
from apps.servers.services.connection import connect
from apps.ssh.errors import CatalogError
from apps.ssh.services import RemoteHost

INSTALL_KIND = "relays.install"
UPDATE_KIND = "relays.update"
REMOVE_KIND = "relays.remove"
KINDS = (INSTALL_KIND, UPDATE_KIND, REMOVE_KIND)


class RelayDeployment:
    """One operation on the relay of a server."""

    def __init__(self, relay: Relay, ctx: OperationContext) -> None:
        self._relay = relay
        self._ctx = ctx

    def install(self) -> None:
        """Prepare the server, check the ports, build and start the container, then update it."""
        plan = RelayPlan.of(self._relay.server)
        _refuse_reserved(plan.ports)
        with ExitStack() as stack:
            host = self._connect(stack)
            setup = HostSetup(host, self._ctx)
            setup.check_os()
            self._place_service_peers()
            plan = RelayPlan.of(self._relay.server)
            config = self._config(plan)
            with self._lock(host):
                setup.prepare()
                container = RelayContainer(host, self._ctx)
                with self._ctx.step(_("Check the ports of the groups")):
                    container.check_ports(plan.ports)
                container.install(plan.ports)
                container.apply(plan.ports, config)
        self._relay.installed_at = timezone.now()
        self._synced(plan)

    def update(self) -> None:
        """Write the groups and members of now; new ports are checked before they are published."""
        with ExitStack() as stack:
            host = self._connect(stack)
            self._place_service_peers()
            plan = RelayPlan.of(self._relay.server)
            _refuse_reserved(plan.ports)
            config = self._config(plan)
            with self._lock(host):
                container = RelayContainer(host, self._ctx)
                with self._ctx.step(_("Check the ports of the groups")):
                    container.check_ports(plan.ports)
                container.apply(plan.ports, config)
        self._synced(plan)

    def remove(self) -> None:
        with ExitStack() as stack:
            host = self._connect(stack)
            with self._lock(host):
                RelayContainer(host, self._ctx).remove()
        self._relay.delete()

    def _connect(self, stack: ExitStack) -> RemoteHost:
        with self._ctx.step(_("Connect to the server")):
            return stack.enter_context(connect(self._relay.server))

    @contextmanager
    def _lock(self, host: RemoteHost) -> Iterator[None]:
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(host.lock())
            yield

    def _place_service_peers(self) -> None:
        """A group whose members cannot take the peer now is left for the next sync."""
        for group, _members in RelayPlan.of(self._relay.server).groups:
            try:
                ServicePeerPlacement(group, self._ctx).place()
            except CatalogError as error:
                self._ctx.log(f"{group.name}: {error.message}")

    def _config(self, plan: RelayPlan) -> bytes:
        with self._ctx.step(_("Prepare the list of members")):
            try:
                config = plan.config()
            except OSError as error:
                raise RelayError(RelayErrorCode.HOST_NOT_RESOLVED, str(error)) from error
            for group in config.groups:
                members = ", ".join(str(backend) for backend in group.backends)
                self._ctx.log(f"{group.port}/udp → {members}")
            return config.to_json().encode()

    def _synced(self, plan: RelayPlan) -> None:
        self._relay.synced = plan.digest()
        self._relay.synced_at = timezone.now()
        self._relay.save(update_fields=["installed_at", "synced", "synced_at"])


def _refuse_reserved(ports: set[int]) -> None:
    """Refused before the panel connects: the port is wrong on any server."""
    reserved = sorted(ports & RESERVED_PORTS)
    if reserved:
        raise RelayError(RelayErrorCode.PORT_RESERVED, f"udp {reserved[0]}")


def start(kind: str, server: Server, *, user: User, ip: str | None = None) -> Operation:
    """Launch an operation on the relay of the server; install gives the server the role first."""
    with transaction.atomic():
        if kind == INSTALL_KIND:
            Relay.objects.get_or_create(server=server)
        elif not Relay.objects.filter(server=server).exists():
            raise RelayError(RelayErrorCode.NOT_INSTALLED, str(server))
        operation = launch(kind, {"server_id": server.pk}, user=user, subject=str(server))
        audit.log(kind, actor=user, target=server, data={"operation": operation.pk}, ip=ip)
    return operation


def sync_relays() -> None:
    """Start an update of every installed relay whose groups changed since it was written."""
    for relay in Relay.objects.filter(installed_at__isnull=False).select_related("server"):
        if _busy(relay) or RelayPlan.of(relay.server).digest() == relay.synced:
            continue
        launch(UPDATE_KIND, {"server_id": relay.server_id}, subject=str(relay.server))


def _busy(relay: Relay) -> bool:
    return Operation.objects.filter(
        kind__in=KINDS,
        status__in=(OperationStatus.PENDING, OperationStatus.RUNNING),
        params__server_id=relay.server_id,
    ).exists()
