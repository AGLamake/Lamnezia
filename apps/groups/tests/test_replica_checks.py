from collections.abc import Callable

import pytest

from amnezia_core.awg import AWG_CONFIG_PATH, AwgServerConfig, Peer
from apps.accounts.models import User
from apps.backups.models import ProtocolBackup
from apps.groups.models import GroupMember
from apps.groups.services.replication import CATCH_UP_KIND, catch_up_lagging, check_replicas
from apps.groups.tests.conftest import JOINER_HOST, LEADER_HOST, Fakes, Run, server_files, succeeded
from apps.keys.models import AccessKey, Drift
from apps.keys.services.actions import KeyActions
from apps.operations.models import Operation
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import OnCommit, Start

pytestmark = pytest.mark.django_db

STRANGER = "dGhpcyBpcyBhIHBlZXIgYWRkZWQgaW4gdGhlIGFwcCE="


@pytest.fixture
def joined(join: Run) -> GroupMember:
    succeeded(join())
    return GroupMember.objects.get(leading=False)


@pytest.fixture
def key(leader: ProtocolInstance, joined: GroupMember, admin: User, start: Start) -> AccessKey:
    """A key of the group, on both servers."""
    operation = start(lambda: KeyActions(admin).issue(leader.source, "Phone", None))
    succeeded(operation)
    return AccessKey.objects.get(pk=operation.params["key_id"])


@pytest.fixture
def catch_up(django_capture_on_commit_callbacks: OnCommit) -> Callable[[], list[Operation]]:
    def run() -> list[Operation]:
        with django_capture_on_commit_callbacks(execute=True):
            catch_up_lagging()
        return list(Operation.objects.filter(kind=CATCH_UP_KIND))

    return run


def edit_member(servers: Fakes, change: Callable[[AwgServerConfig], object]) -> None:
    """Change awg0.conf of the member by hand, past the panel."""
    config = AwgServerConfig.parse(server_files(servers[JOINER_HOST]).config)
    change(config)
    servers[JOINER_HOST].container_files[AWG_CONFIG_PATH] = config.serialize().encode()


def lagging(member: GroupMember) -> bool:
    member.refresh_from_db()
    return member.lagging


def test_key_change_saves_a_copy_of_every_member(
    servers: Fakes, joined: GroupMember, key: AccessKey
) -> None:
    for instance_id in (joined.instance_id, GroupMember.objects.get(leading=True).instance_id):
        backup = ProtocolBackup.objects.get(instance_id=instance_id)
        assert AwgServerConfig.parse(backup.config).find_peer(key.public_key) is not None


def test_member_in_line_with_the_group_does_not_lag(
    servers: Fakes, joined: GroupMember, key: AccessKey
) -> None:
    check_replicas()

    assert not lagging(joined)


def test_peer_removed_by_hand_on_a_member_comes_back_with_the_catch_up(
    servers: Fakes,
    joined: GroupMember,
    key: AccessKey,
    catch_up: Callable[[], list[Operation]],
) -> None:
    edit_member(servers, lambda config: config.remove_peer(key.public_key))

    check_replicas()
    assert lagging(joined)
    (operation,) = catch_up()

    succeeded(operation)
    member = AwgServerConfig.parse(server_files(servers[JOINER_HOST]).config)
    assert member.find_peer(key.public_key) is not None
    assert not lagging(joined)


def test_member_drift_changes_no_key_of_the_panel(
    servers: Fakes, leader: ProtocolInstance, joined: GroupMember, key: AccessKey
) -> None:
    edit_member(servers, lambda config: config.add_peer(Peer(STRANGER, ("10.8.1.200/32",))))
    keys = list(AccessKey.objects.values_list("pk", "status", "public_key"))

    check_replicas()

    assert lagging(joined)
    assert list(AccessKey.objects.values_list("pk", "status", "public_key")) == keys
    assert not Drift.objects.exists()
    leader_config = AwgServerConfig.parse(server_files(servers[LEADER_HOST]).config)
    assert leader_config.find_peer(STRANGER) is None


def test_unreachable_member_is_not_marked_lagging(
    servers: Fakes, down: set[str], joined: GroupMember, key: AccessKey
) -> None:
    edit_member(servers, lambda config: config.remove_peer(key.public_key))
    down.add(JOINER_HOST)

    check_replicas()

    assert not lagging(joined)
