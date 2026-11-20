"""The change of a group's routing between forward and split (docs/SPLIT.md 6, step 2).

A background operation: it reads the client counters of the members and of the clone first,
so the traffic of the old path is counted up to the change, gives a split group its uplink on
every member, saves the routing and updates the relay. The clone counts the keys from their
first byte, and a group forwarded again leaves its clone counters at zero
(services/traffic.py). The agent then moves the tunnel of the clients to
the clone or back; each client makes one new handshake. A group that goes back to forward
loses its uplink only after the agent stops using it.
"""

from django.db import transaction
from django.utils.translation import gettext as _

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.groups.models import GroupRouting, ServerGroup
from apps.groups.services.replication import backends
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.relays.errors import RelayError, RelayErrorCode
from apps.relays.models import PeerRole, Relay
from apps.relays.services.deployment import KINDS, ROUTING_KIND, RelayDeployment
from apps.relays.services.service_peers import ServicePeerPlacement
from apps.relays.services.split_lists import add_default_source, update_soon
from apps.relays.services.traffic import (
    expect_group_on_clone,
    relay_target_name,
    reset_clone,
)
from apps.ssh.errors import SshError
from apps.traffic.services.poller import TrafficPoller
from apps.traffic.services.targets import poll_targets


def start_routing(
    group: ServerGroup, routing: GroupRouting, *, user: User, ip: str | None = None
) -> Operation:
    """Start moving the group to `routing`; refused while another relay operation runs.

    A group split for the first time gets the default source of its network list.
    """
    with transaction.atomic():
        group = ServerGroup.objects.select_for_update().get(pk=group.pk)
        installed = Relay.objects.select_for_update().filter(installed_at__isnull=False)
        relay = installed.filter(server_id=group.relay_id).first() if group.relay_id else None
        if relay is None:
            raise RelayError(RelayErrorCode.NOT_INSTALLED, group.name)
        if group.routing == routing:
            raise RelayError(RelayErrorCode.ROUTING_SAME, group.name)
        running = Operation.objects.filter(
            kind__in=KINDS,
            status__in=(OperationStatus.PENDING, OperationStatus.RUNNING),
            params__server_id=relay.server_id,
        )
        if running.exists():
            raise RelayError(RelayErrorCode.RELAY_BUSY, str(relay.server))
        if routing == GroupRouting.SPLIT and add_default_source(group) is not None:
            update_soon()
        params = {"server_id": relay.server_id, "group_id": group.pk, "routing": str(routing)}
        operation = launch(ROUTING_KIND, params, user=user, subject=str(group))
        data = {"operation": operation.pk, "routing": str(routing)}
        audit.log(ROUTING_KIND, actor=user, target=group, data=data, ip=ip)
    return operation


class RoutingChange:
    """One change of the routing of a group behind its relay."""

    def __init__(
        self, relay: Relay, group: ServerGroup, routing: GroupRouting, ctx: OperationContext
    ) -> None:
        self._relay = relay
        self._group = group
        self._routing = routing
        self._ctx = ctx

    def run(self) -> None:
        """A split group whose leading server does not answer fails before anything changes;
        a relay that does not answer after the routing is saved gets it on the next sync."""
        self._read_counters()
        split = self._routing == GroupRouting.SPLIT
        if split:
            ServicePeerPlacement(self._group, self._ctx).place([PeerRole.PROBE, PeerRole.UPLINK])
            expect_group_on_clone(self._group)
        title = _("Save the routing of %(group)s") % {"group": self._group.name}
        with self._ctx.step(title):
            self._group.routing = self._routing
            self._group.save(update_fields=["routing"])
            self._ctx.log(self._group.get_routing_display())
        if not split:
            # Read just before, and no longer polled: the routing is saved.
            reset_clone(self._group)
        RelayDeployment(self._relay, self._ctx).update()

    def _read_counters(self) -> None:
        """Count the traffic of the members and the clones of the relay up to the change; one
        that does not answer is counted on the next reading, as when it is down."""
        with self._ctx.step(_("Read the traffic counters of the members")):
            names = [member.instance.server.name for member in backends(self._group)]
            for name in [*names, relay_target_name(self._relay.server)]:
                target = poll_targets.get(name)
                if target is None:
                    continue
                try:
                    TrafficPoller().poll(target)
                except SshError as error:
                    self._ctx.log(f"{name}: {error.message}")
