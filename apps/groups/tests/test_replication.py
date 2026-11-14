from collections.abc import Callable
from typing import Any

import pytest

from amnezia_core.awg import AwgServerConfig, Peer
from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.groups.models import GroupMember, ServerGroup
from apps.groups.services.replication import CATCH_UP_KIND, backends, catch_up_lagging
from apps.groups.tests.conftest import (
    JOINER_HOST,
    LEADER_HOST,
    Fakes,
    Run,
    server_files,
    succeeded,
)
from apps.keys.errors import PeerErrorCode
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.actions import KeyActions
from apps.keys.services.peers import PeerManager
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import OnCommit, Start, issued_key

pytestmark = pytest.mark.django_db

Issue = Callable[[str], AccessKey]


@pytest.fixture
def joined(join: Run) -> GroupMember:
    """The standalone server, now a member of the group with the leader."""
    succeeded(join())
    return GroupMember.objects.get(leading=False)


@pytest.fixture
def issue(leader: ProtocolInstance, admin: User, start: Start) -> Issue:
    """Issue a key of the group and return it as saved after the operation."""

    def run(name: str) -> AccessKey:
        operation = start(lambda: KeyActions(admin).issue(leader.source, name, None))
        succeeded(operation)
        return AccessKey.objects.get(pk=operation.params["key_id"])

    return run


@pytest.fixture
def catch_up(django_capture_on_commit_callbacks: OnCommit) -> Callable[[], list[Operation]]:
    """Run the periodic task and the catch-ups it starts; the operations started."""

    def run() -> list[Operation]:
        with django_capture_on_commit_callbacks(execute=True):
            catch_up_lagging()
        return list(Operation.objects.filter(kind=CATCH_UP_KIND))

    return run


def peer(servers: Fakes, host: str, key: AccessKey) -> Peer | None:
    return AwgServerConfig.parse(server_files(servers[host]).config).find_peer(key.public_key)


def lagging(member: GroupMember) -> bool:
    member.refresh_from_db()
    return member.lagging


def test_key_is_issued_on_every_member_with_one_address(
    servers: Fakes, joined: GroupMember, issue: Issue
) -> None:
    key = issue("Phone")

    on_leader, on_member = peer(servers, LEADER_HOST, key), peer(servers, JOINER_HOST, key)
    assert on_leader is not None
    assert on_member == on_leader
    assert [str(address) for address in on_leader.addresses] == [key.client_ip]
    assert not lagging(joined)


def test_issue_with_a_member_down_marks_it_lagging(
    servers: Fakes, down: set[str], group: ServerGroup, joined: GroupMember, issue: Issue
) -> None:
    down.add(JOINER_HOST)

    key = issue("Phone")

    assert key.status == KeyStatus.ACTIVE
    assert peer(servers, LEADER_HOST, key) is not None
    assert peer(servers, JOINER_HOST, key) is None
    assert lagging(joined)
    assert [member.pk for member in backends(group)] == [group.members.get(leading=True).pk]


def test_lagging_member_catches_up_with_the_group(  # noqa: PLR0913, PLR0917
    servers: Fakes,
    down: set[str],
    group: ServerGroup,
    joined: GroupMember,
    issue: Issue,
    admin: User,
    start: Start,
    catch_up: Callable[[], list[Operation]],
) -> None:
    old = issue("Laptop")
    down.add(JOINER_HOST)
    new = issue("Phone")
    succeeded(start(lambda: KeyActions(admin).revoke(old)))
    down.clear()

    (operation,) = catch_up()

    succeeded(operation)
    assert server_files(servers[JOINER_HOST]) == server_files(servers[LEADER_HOST])
    assert peer(servers, JOINER_HOST, new) is not None
    assert peer(servers, JOINER_HOST, old) is None
    assert not lagging(joined)
    assert len(backends(group)) == 2


def test_unreachable_lagging_member_waits(
    down: set[str],
    joined: GroupMember,
    issue: Issue,
    catch_up: Callable[[], list[Operation]],
) -> None:
    down.add(JOINER_HOST)
    issue("Phone")

    assert catch_up() == []
    assert lagging(joined)


def test_no_member_answers_nothing_changes(
    servers: Fakes, down: set[str], joined: GroupMember, leader: ProtocolInstance, start: Start
) -> None:
    down.update({LEADER_HOST, JOINER_HOST})
    admin = UserFactory()

    operation = start(lambda: KeyActions(admin).issue(leader.source, "Phone", None))

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == PeerErrorCode.NO_GROUP_SERVER
    assert not GroupMember.objects.exclude(lagging_since=None).exists()


def test_key_issued_while_a_server_joins_reaches_it_after_the_catch_up(
    servers: Fakes,
    join: Run,
    issue: Issue,
    monkeypatch: pytest.MonkeyPatch,
    catch_up: Callable[[], list[Operation]],
) -> None:
    """The issue chose its members before the join, and changes the leader after it: the
    leader was locked for the join meanwhile.
    """
    add = PeerManager.add

    def add_after_join(manager: PeerManager, *args: Any, **kwargs: Any) -> Peer:
        monkeypatch.setattr(PeerManager, "add", add)
        succeeded(join())
        return add(manager, *args, **kwargs)

    monkeypatch.setattr(PeerManager, "add", add_after_join)
    key = issue("Phone")
    member = GroupMember.objects.get(leading=False)
    assert peer(servers, JOINER_HOST, key) is None
    assert lagging(member)

    (operation,) = catch_up()

    succeeded(operation)
    assert peer(servers, JOINER_HOST, key) == peer(servers, LEADER_HOST, key)


def test_users_of_a_joined_server_get_keys_of_the_group(
    servers: Fakes, leader: ProtocolInstance, joiner: ProtocolInstance, join: Run
) -> None:
    owner = UserFactory()
    issued_key(joiner, owner)

    succeeded(join())

    key = AccessKey.objects.get(source=leader.source, user=owner)
    assert key.status == KeyStatus.ACTIVE
    assert key.name == "iPhone"
    assert peer(servers, LEADER_HOST, key) is not None
    assert peer(servers, JOINER_HOST, key) is not None
    notification = Notification.objects.get(event=Event.KEYS_UPDATED)
    assert (notification.user, notification.data["server"]) == (owner, joiner.server.name)
