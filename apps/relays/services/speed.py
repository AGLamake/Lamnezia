"""Speed limits of relayed groups on the member the relay forwards their clients to.

The rules of the keys of a group are those of its leading server. In forward routing, and in
the emergency forward of a split group, the relay may send the clients to another member: by the
last reading of the relay that member stands in for the leading server, and the traffic app puts
the limits there. In split routing the clients are limited on the clone and reach the member
through the uplink, so no member stands in. A reading that moves the clients of a group to
another member or mode applies the limits at once, not at the next run of the traffic app.
"""

from collections.abc import Iterator

from django.db import transaction

from amnezia_core.relay import Routing
from apps.groups.models import GroupMember
from apps.protocols.models import ProtocolInstance
from apps.relays.models import Relay
from apps.relays.services.health import GroupHealth, RelayHealth
from apps.traffic.services.speed import speed_stand_ins
from apps.traffic.tasks import apply_speed_limits


@speed_stand_ins.register
def forwarded_members() -> Iterator[tuple[str, str]]:
    """The active member of every forwarded group that is not its leading server, with the
    source of the keys of the group."""
    active: dict[int, int] = {}  # member id: the port of its group on the relay
    servers: dict[int, int] = {}  # member id: the relay server
    for relay in Relay.objects.filter(installed_at__isnull=False):
        for health in relay.groups_health:
            member_id = _forwarded_to(health)
            if member_id is not None:
                active[member_id], servers[member_id] = health.port, relay.server_id
    if not active:
        return
    members = GroupMember.objects.filter(pk__in=active, leading=False).select_related("group")
    current = [
        member
        for member in members
        if member.group.relay_id == servers[member.pk] and member.group.port == active[member.pk]
    ]
    leaders = GroupMember.objects.filter(
        group_id__in=[member.group_id for member in current], leading=True
    ).values_list("group_id", "instance_id")
    keys = {group_id: ProtocolInstance.source_for(instance_id) for group_id, instance_id in leaders}
    for member in current:
        if member.group_id in keys:
            yield ProtocolInstance.source_for(member.instance_id), keys[member.group_id]


def _forwarded_to(health: GroupHealth) -> int | None:
    """The member the relay forwards the clients of the group to; None in split routing."""
    if health.mode is not Routing.FORWARD or health.active is None:
        return None
    return health.active.id


def follow_clients(before: RelayHealth | None, after: RelayHealth) -> None:
    """Apply the speed limits when the relay forwards a group to another member than in
    `before`, or forwards it no longer."""
    if _forwarding(before) != _forwarding(after):
        transaction.on_commit(apply_speed_limits.delay)


def _forwarding(health: RelayHealth | None) -> dict[int, int | None]:
    groups = health.groups if health else {}
    return {port: _forwarded_to(group) for port, group in groups.items()}
