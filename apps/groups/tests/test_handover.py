import json
from collections.abc import Callable

import pytest

from amnezia_core.awg import AWG_CONFIG_PATH, AwgServerConfig, Peer
from amnezia_core.awg_group import AwgGroupIdentity
from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.groups.errors import GroupError, GroupErrorCode
from apps.groups.models import GroupMember, ServerGroup
from apps.groups.services.handover import start_handover
from apps.groups.services.membership import start_leave
from apps.groups.tests.conftest import (
    JOINER_HOST,
    LEADER_HOST,
    Fakes,
    Run,
    server_files,
    succeeded,
)
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.actions import KeyActions
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import Start
from apps.traffic.models import TrafficHourly

pytestmark = pytest.mark.django_db

Issue = Callable[[ProtocolInstance, str], AccessKey]


@pytest.fixture
def joined(join: Run) -> GroupMember:
    succeeded(join())
    return GroupMember.objects.get(leading=False)


@pytest.fixture
def issue(admin: User, start: Start) -> Issue:
    """Issue a key on the instance and return it as saved after the operation."""

    def run(instance: ProtocolInstance, name: str) -> AccessKey:
        owner = UserFactory()
        operation = start(lambda: KeyActions(admin).issue(instance.source, name, owner))
        succeeded(operation)
        return AccessKey.objects.get(pk=operation.params["key_id"])

    return run


@pytest.fixture
def hand_over(admin: User, start: Start) -> Callable[[GroupMember], Operation]:
    return lambda member: start(lambda: start_handover(member, user=admin))


def peer(servers: Fakes, host: str, key: AccessKey) -> Peer | None:
    return AwgServerConfig.parse(server_files(servers[host]).config).find_peer(key.public_key)


def leader_of(group: ServerGroup) -> ProtocolInstance:
    leader = group.leader
    assert leader is not None
    return leader.instance


def test_keys_are_issued_on_the_new_leader_and_reach_every_member(  # noqa: PLR0913, PLR0917
    servers: Fakes,
    group: ServerGroup,
    leader: ProtocolInstance,
    joiner: ProtocolInstance,
    joined: GroupMember,
    issue: Issue,
    hand_over: Callable[[GroupMember], Operation],
) -> None:
    before = issue(leader, "Laptop")

    succeeded(hand_over(joined))

    assert leader_of(group) == joiner
    before.refresh_from_db()
    assert (before.source, before.status) == (joiner.source, KeyStatus.ACTIVE)
    after = issue(joiner, "Phone")
    for key in (before, after):
        assert peer(servers, LEADER_HOST, key) == peer(servers, JOINER_HOST, key) is not None
    assert not GroupMember.objects.exclude(lagging_since=None).exists()


def test_former_leader_leaves_with_a_clean_amneziawg(  # noqa: PLR0913, PLR0917
    servers: Fakes,
    group: ServerGroup,
    leader: ProtocolInstance,
    joined: GroupMember,
    issue: Issue,
    admin: User,
    start: Start,
    hand_over: Callable[[GroupMember], Operation],
) -> None:
    key = issue(leader, "Laptop")
    succeeded(hand_over(joined))
    former = GroupMember.objects.get(instance=leader)

    succeeded(start(lambda: start_leave(former, user=admin)))

    assert list(group.members.all()) == [joined]
    left = AwgServerConfig.parse(server_files(servers[LEADER_HOST]).config)
    assert left.peers == []
    leader.refresh_from_db()
    assert leader.private_key == left.private_key
    joiner_config = AwgServerConfig.parse(server_files(servers[JOINER_HOST]).config)
    assert leader.private_key != joiner_config.private_key
    key.refresh_from_db()
    assert key.status == KeyStatus.ACTIVE
    assert peer(servers, JOINER_HOST, key) is not None


def test_server_that_differs_from_the_group_does_not_lead_it(
    servers: Fakes,
    group: ServerGroup,
    leader: ProtocolInstance,
    joined: GroupMember,
    hand_over: Callable[[GroupMember], Operation],
) -> None:
    files = server_files(servers[JOINER_HOST])
    config = AwgServerConfig.parse(files.config)
    config.remove_peer(config.peers[0].public_key)
    servers[JOINER_HOST].container_files[AWG_CONFIG_PATH] = config.serialize().encode()

    operation = hand_over(joined)

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == GroupErrorCode.DIVERGED
    assert leader_of(group) == leader


def test_leader_down_hands_over_and_lags(  # noqa: PLR0913, PLR0917
    down: set[str],
    group: ServerGroup,
    leader: ProtocolInstance,
    joiner: ProtocolInstance,
    joined: GroupMember,
    hand_over: Callable[[GroupMember], Operation],
) -> None:
    down.add(LEADER_HOST)

    succeeded(hand_over(joined))

    assert leader_of(group) == joiner
    former = GroupMember.objects.get(instance=leader)
    assert former.lagging


def test_leader_down_and_other_keys_on_the_server_refuse(
    down: set[str],
    group: ServerGroup,
    leader: ProtocolInstance,
    joined: GroupMember,
    hand_over: Callable[[GroupMember], Operation],
) -> None:
    down.add(LEADER_HOST)
    ProtocolInstance.objects.filter(pk=leader.pk).update(preshared_key="other")

    operation = hand_over(joined)

    assert operation.error_code == GroupErrorCode.DIVERGED
    assert leader_of(group) == leader


@pytest.mark.parametrize("lagging", [True, False])
def test_lagging_or_leading_server_is_refused(
    group: ServerGroup, joined: GroupMember, admin: User, lagging: bool
) -> None:
    if lagging:
        GroupMember.objects.filter(pk=joined.pk).update(lagging_since="2026-11-15T00:00Z")
        candidate = joined
    else:
        candidate = GroupMember.objects.get(leading=True)

    with pytest.raises(GroupError) as raised:
        start_handover(candidate, user=admin)

    assert raised.value.code == GroupErrorCode.NOT_UP_TO_DATE


def test_handover_waits_for_key_operations(
    group: ServerGroup, leader: ProtocolInstance, joined: GroupMember, admin: User
) -> None:
    KeyActions(admin).issue(leader.source, "Phone", None)

    with pytest.raises(GroupError) as raised:
        start_handover(joined, user=admin)

    assert raised.value.code == GroupErrorCode.KEYS_CHANGING


def test_traffic_of_the_keys_moves_with_them(
    leader: ProtocolInstance,
    joiner: ProtocolInstance,
    joined: GroupMember,
    issue: Issue,
    hand_over: Callable[[GroupMember], Operation],
) -> None:
    key = issue(leader, "Laptop")
    TrafficHourly.objects.create(
        source=leader.source, public_key=key.public_key, hour="2026-11-15T10:00Z", rx_bytes=5
    )

    succeeded(hand_over(joined))

    row = TrafficHourly.objects.get(public_key=key.public_key)
    assert (row.source, row.rx_bytes) == (joiner.source, 5)


def test_keys_without_a_relay_move_to_the_new_leader_address(
    leader: ProtocolInstance,
    joiner: ProtocolInstance,
    joined: GroupMember,
    issue: Issue,
    hand_over: Callable[[GroupMember], Operation],
) -> None:
    key = issue(leader, "Laptop")
    assert json.loads(key.profile)["host"] == LEADER_HOST

    succeeded(hand_over(joined))

    key.refresh_from_db()
    assert json.loads(key.profile)["host"] == JOINER_HOST
    notification = Notification.objects.get(event=Event.KEYS_UPDATED, user=key.user)
    assert notification.data["server"] == joiner.server.name


def test_handover_run_again_does_nothing(  # noqa: PLR0913, PLR0917
    servers: Fakes,
    group: ServerGroup,
    joiner: ProtocolInstance,
    joined: GroupMember,
    hand_over: Callable[[GroupMember], Operation],
    start: Start,
    admin: User,
) -> None:
    operation = hand_over(joined)
    succeeded(operation)
    files = server_files(servers[JOINER_HOST])

    again = start(lambda: launch(operation.kind, operation.params, user=admin))

    succeeded(again)
    assert leader_of(group) == joiner
    assert AwgGroupIdentity(files).compare(server_files(servers[JOINER_HOST])) == []
