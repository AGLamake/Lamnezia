"""Speed limits of a relayed group on the member the relay forwards its clients to."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import pytest
from django.utils import timezone

from amnezia_core.relay import Routing
from apps.groups.models import GroupMember, ServerGroup
from apps.protocols.models import ProtocolInstance
from apps.relays.models import Relay
from apps.relays.services.health import GroupHealth, Member, RelayHealth
from apps.relays.services.speed import follow_clients
from apps.traffic.services.speed import speed_stand_ins

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[list[Callable[[], Any]]]]


def reading(
    group: ServerGroup,
    active: GroupMember | None,
    *,
    routing: Routing = Routing.FORWARD,
    mode: Routing = Routing.FORWARD,
) -> RelayHealth:
    health = GroupHealth(
        port=group.port,
        active=Member(active.pk, str(active.instance.server)) if active else None,
        switched_at=None,
        alive=(),
        down=(),
        bytes_from_clients=0,
        bytes_to_clients=0,
        routing=routing,
        mode=mode,
    )
    return RelayHealth(1_700_000_000, {group.port: health})


def read(group: ServerGroup, health: RelayHealth, *, installed: bool = True) -> None:
    assert group.relay is not None
    Relay.objects.update_or_create(
        server=group.relay,
        defaults={
            "installed_at": timezone.now() if installed else None,
            "health": health.to_dict(),
        },
    )


def test_a_member_the_relay_forwards_to_carries_the_keys_of_the_leader(
    relayed: ServerGroup, joined: GroupMember, leader: ProtocolInstance
) -> None:
    read(relayed, reading(relayed, joined))

    assert dict(speed_stand_ins) == {joined.instance.source: leader.source}


def test_the_emergency_forward_of_a_split_group_moves_the_limits_to_the_member(
    relayed: ServerGroup, joined: GroupMember, leader: ProtocolInstance
) -> None:
    read(relayed, reading(relayed, joined, routing=Routing.SPLIT, mode=Routing.FORWARD))

    assert dict(speed_stand_ins) == {joined.instance.source: leader.source}


@pytest.mark.parametrize(
    ("leading", "mode", "installed"),
    [
        (True, Routing.FORWARD, True),  # the leader has its own limits
        (False, Routing.SPLIT, True),  # the clone limits the clients
        (False, Routing.FORWARD, False),  # the relay is not installed
    ],
)
def test_no_member_stands_in_when_the_clients_are_limited_elsewhere(
    relayed: ServerGroup, leading: bool, mode: Routing, installed: bool
) -> None:
    member = GroupMember.objects.get(group=relayed, leading=leading)
    read(relayed, reading(relayed, member, routing=mode, mode=mode), installed=installed)

    assert dict(speed_stand_ins) == {}


def test_a_reading_of_a_member_of_another_group_is_ignored(
    relayed: ServerGroup, joined: GroupMember
) -> None:
    health = reading(relayed, joined)
    ServerGroup.objects.filter(pk=relayed.pk).update(port=relayed.port + 1)
    read(relayed, health)

    assert dict(speed_stand_ins) == {}


def test_limits_are_applied_at_once_when_the_clients_move(
    relayed: ServerGroup, joined: GroupMember, django_capture_on_commit_callbacks: OnCommit
) -> None:
    leader = GroupMember.objects.get(group=relayed, leading=True)
    on_leader, on_joined = reading(relayed, leader), reading(relayed, joined)
    split = reading(relayed, joined, routing=Routing.SPLIT, mode=Routing.SPLIT)

    with django_capture_on_commit_callbacks() as moved:
        follow_clients(on_leader, on_joined)
        follow_clients(on_joined, split)
        follow_clients(None, on_leader)
    with django_capture_on_commit_callbacks() as stayed:
        follow_clients(on_joined, reading(relayed, joined))

    assert len(moved) == 3
    assert stayed == []
