from collections.abc import Callable

import pytest

from amnezia_core.awg import AWG_CLIENTS_TABLE_PATH, AwgServerConfig
from amnezia_core.awg_group import AwgGroupIdentity
from amnezia_core.clients_table import ClientsTable
from apps.accounts.models import User
from apps.backups.models import ProtocolBackup, ProtocolSnapshot, SnapshotReason
from apps.groups.models import GroupMember, MemberState, ServerGroup
from apps.groups.services.membership import start_join, start_leave
from apps.groups.tests.conftest import JOINER_HOST, LEADER_HOST, server_files
from apps.keys.models import KeyStatus
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import Start, issued_key
from apps.protocols.tests.fake_awg import FakeAwgServer

pytestmark = pytest.mark.django_db

Run = Callable[[], Operation]
Fakes = dict[str, FakeAwgServer]


@pytest.fixture
def join(
    servers: Fakes, group: ServerGroup, joiner: ProtocolInstance, admin: User, start: Start
) -> Run:
    """Run the joining of the standalone server into the group."""
    return lambda: start(lambda: start_join(group, joiner, user=admin))


@pytest.fixture
def leave(joiner: ProtocolInstance, admin: User, start: Start) -> Run:
    """Run the leaving of the joined server."""
    return lambda: start(lambda: start_leave(GroupMember.objects.get(instance=joiner), user=admin))


def succeeded(operation: Operation) -> None:
    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message


def is_clone(servers: Fakes) -> bool:
    leader_files = server_files(servers[LEADER_HOST])
    return AwgGroupIdentity(leader_files).compare(server_files(servers[JOINER_HOST])) == []


def test_joined_server_is_a_clone_of_the_leader(
    servers: Fakes, leader: ProtocolInstance, joiner: ProtocolInstance, join: Run
) -> None:
    old_key = issued_key(joiner)

    succeeded(join())

    assert is_clone(servers)
    assert servers[JOINER_HOST].awg_up
    member = GroupMember.objects.get(instance=joiner)
    assert member.state == MemberState.MEMBER
    assert member.joined_at is not None
    joiner.refresh_from_db()
    assert joiner.private_key == leader.private_key
    assert joiner.public_key == leader.public_key
    assert joiner.preshared_key == leader.preshared_key
    assert joiner.params == leader.params
    old_key.refresh_from_db()
    assert old_key.status == KeyStatus.REVOKED
    assert ProtocolBackup.objects.filter(instance=joiner).exists()


def test_server_with_clients_is_saved_before_it_joins(
    servers: Fakes, joiner: ProtocolInstance, join: Run
) -> None:
    before = server_files(servers[JOINER_HOST])

    succeeded(join())

    snapshot = ProtocolSnapshot.objects.get(instance=joiner)
    assert snapshot.reason == SnapshotReason.GROUP_JOIN
    assert snapshot.config == before.config
    assert snapshot.clients_table == before.clients_table
    assert snapshot.preshared_key == before.preshared_key


def test_clean_server_joins_without_a_snapshot(servers: Fakes, join: Run) -> None:
    clean = ClientsTable([]).serialize().encode()
    servers[JOINER_HOST].container_files[AWG_CLIENTS_TABLE_PATH] = clean

    succeeded(join())

    assert not ProtocolSnapshot.objects.exists()
    assert is_clone(servers)


def test_server_running_other_keys_than_the_panel_knows_is_not_replaced(
    servers: Fakes, leader: ProtocolInstance, joiner: ProtocolInstance, join: Run
) -> None:
    before = server_files(servers[JOINER_HOST])
    joiner.private_key = leader.private_key
    joiner.save()

    operation = join()

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == "other_server_key"
    assert server_files(servers[JOINER_HOST]) == before
    assert GroupMember.objects.get(instance=joiner).state == MemberState.JOINING


def test_left_server_gets_a_clean_amneziawg(
    servers: Fakes, leader: ProtocolInstance, joiner: ProtocolInstance, join: Run, leave: Run
) -> None:
    succeeded(join())
    group_files = server_files(servers[JOINER_HOST])

    succeeded(leave())

    assert not GroupMember.objects.filter(instance=joiner).exists()
    files = server_files(servers[JOINER_HOST])
    config = AwgServerConfig.parse(files.config)
    assert config.private_key != leader.private_key
    assert config.peers == []
    assert ClientsTable.parse(files.clients_table).client_ids == []
    assert servers[JOINER_HOST].awg_up
    joiner.refresh_from_db()
    assert joiner.private_key == config.private_key
    snapshot = ProtocolSnapshot.objects.get(instance=joiner, reason=SnapshotReason.GROUP_LEAVE)
    assert snapshot.config == group_files.config
