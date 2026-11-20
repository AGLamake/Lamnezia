"""Traffic of split groups: the clone as a source of the keys, the uplink as the exit load.

In split routing the clients of a group are peers of its clone on the relay (docs/SPLIT.md
4.2), so the panel reads the clone like a member: its peers count for the keys of the leading
server. The uplink on the members carries the traffic of all those clients once more; it counts
for a source of its own, the exit load of the group, and not for the keys. The members are read
as before: a key still on the old address, or the forward fallback of the agent, sends its
traffic there, and a byte takes one path only.

A change of routing reads the clone with the members first. A group that goes back to forward
leaves its clone counters at zero, so a later split counts its clients from the first byte.
"""

from collections.abc import Iterator, Sequence
from functools import partial
from itertools import groupby

from django.utils import timezone

from amnezia_core.relay import clone_interface
from apps.groups.models import GroupMember, GroupRouting, ServerGroup
from apps.keys.models import AccessKey, KeyStatus
from apps.protocols.models import ProtocolInstance
from apps.relays.models import PeerRole, ServicePeer
from apps.relays.services.container import NAME
from apps.servers.models import Server
from apps.servers.services.connection import connect
from apps.traffic.models import TrafficCounter, TrafficSource
from apps.traffic.services.recorder import TrafficRecorder
from apps.traffic.services.targets import PollTarget, peer_owners, poll_targets


def clone_source(group_id: int) -> str:
    """The traffic source of the clone of a split group."""
    return f"clone:{group_id}"


def uplink_source(group_id: int) -> str:
    """The traffic source of the uplink of a split group on its members: the exit load."""
    return f"uplink:{group_id}"


def relay_target_name(server: Server) -> str:
    """The name of the poll target of the clones on a relay; the server's own is its name."""
    return f"{server.name} (relay)"


def split_groups() -> list[tuple[ServerGroup, str]]:
    """The split groups on installed relays with their uplink, and the source of their keys."""
    groups = ServerGroup.objects.filter(
        routing=GroupRouting.SPLIT,
        relay__relay_role__installed_at__isnull=False,
        service_peers__role=PeerRole.UPLINK,
    ).distinct()
    leaders = dict(
        GroupMember.objects.filter(group__in=groups, leading=True).values_list(
            "group_id", "instance_id"
        )
    )
    return [
        (group, ProtocolInstance.source_for(leaders[group.pk]))
        for group in groups.select_related("relay").order_by("relay_id", "port")
        if group.pk in leaders
    ]


@poll_targets.register
def clone_targets() -> Iterator[PollTarget]:
    """One target per relay with split groups: the clone interface of each in the container."""
    for _relay, found in groupby(split_groups(), key=lambda item: item[0].relay_id):
        groups = list(found)
        server = groups[0][0].relay
        if server is None:
            continue
        sources = {clone_source(group.pk): (group, keys) for group, keys in groups}
        yield PollTarget(
            relay_target_name(server),
            partial(connect, server),
            dict.fromkeys(sources, NAME),
            {source: keys for source, (_group, keys) in sources.items()},
            {source: clone_interface(group.port) for source, (group, _keys) in sources.items()},
        )


@peer_owners.register
def uplink_owners() -> Iterator[tuple[str, str]]:
    """The uplink of every split group: its traffic is the exit load, not that of a key."""
    uplinks = ServicePeer.objects.filter(role=PeerRole.UPLINK).values_list("public_key", "group_id")
    for public_key, group_id in uplinks:
        yield public_key, uplink_source(group_id)


def expect_new_keys(keys_source: str, public_keys: Sequence[str]) -> None:
    """New keys of a leading server count from their first byte on the clone of its split
    group too, as they do on the server."""
    for group, source in split_groups():
        if source == keys_source:
            _expect(group.pk, keys_source, public_keys)


def expect_group_on_clone(group: ServerGroup) -> None:
    """Before a group is split: every live key of the group counts on the clone from zero."""
    leader = GroupMember.objects.filter(group=group, leading=True).first()
    if leader is None:
        return
    keys_source = ProtocolInstance.source_for(leader.instance_id)
    keys = AccessKey.objects.filter(source=keys_source, status=KeyStatus.ACTIVE)
    _expect(group.pk, keys_source, list(keys.values_list("public_key", flat=True)))


def reset_clone(group: ServerGroup) -> None:
    """The group is forwarded again: the clone goes, and a later one counts from zero."""
    source = clone_source(group.pk)
    TrafficCounter.objects.filter(source=source).update(rx_bytes=0, tx_bytes=0, present=False)
    TrafficSource.objects.filter(source=source).update(available=True, started="")


def _expect(group_id: int, keys_source: str, public_keys: Sequence[str]) -> None:
    recorder, at = TrafficRecorder(), timezone.now()
    for public_key in public_keys:
        recorder.expect_peer(clone_source(group_id), public_key, at, keys_source)
