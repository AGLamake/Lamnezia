import pytest

from amnezia_core.awg import AWG_CLIENTS_TABLE_PATH, AwgServerConfig
from amnezia_core.awg_group import AwgGroupIdentity, standalone_files
from amnezia_core.clients_table import ClientsTable
from apps.accounts.models import User
from apps.backups.models import ProtocolBackup, ProtocolSnapshot, SnapshotReason
from apps.groups.errors import GroupError, GroupErrorCode
from apps.groups.models import GroupMember, LagReason, MemberState, ServerGroup
from apps.groups.services.membership import (
    cancel_join,
    join_failed,
    leave_failed,
    retry_join,
    retry_leave,
    start_join,
    start_leave,
    stay_in_group,
)
from apps.groups.services.replication import CATCH_UP_KIND, backends
from apps.groups.tests.conftest import (
    JOINER_HOST,
    LEADER_HOST,
    Fakes,
    Run,
    server_files,
    succeeded,
)
from apps.keys.models import KeyStatus
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import APP_CONFIG, OnCommit, Start, issued_key

pytestmark = pytest.mark.django_db


@pytest.fixture
def leave(joiner: ProtocolInstance, admin: User, start: Start) -> Run:
    """Run the leaving of the joined server."""
    return lambda: start(lambda: start_leave(GroupMember.objects.get(instance=joiner), user=admin))


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


def test_joined_server_takes_the_dns_of_the_group(
    leader: ProtocolInstance, joiner: ProtocolInstance, join: Run
) -> None:
    leader.primary_dns, leader.secondary_dns = "9.9.9.9", "149.112.112.112"
    leader.save(update_fields=["primary_dns", "secondary_dns"])
    joiner.primary_dns, joiner.secondary_dns = "1.1.1.1", "1.0.0.1"
    joiner.save(update_fields=["primary_dns", "secondary_dns"])

    succeeded(join())

    joiner.refresh_from_db()
    assert joiner.dns == ("9.9.9.9", "149.112.112.112")


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


def test_failed_leave_is_retried_to_a_clean_amneziawg(  # noqa: PLR0913, PLR0917
    servers: Fakes,
    leader: ProtocolInstance,
    joiner: ProtocolInstance,
    admin: User,
    down: set[str],
    start: Start,
    join: Run,
    leave: Run,
) -> None:
    succeeded(join())
    down.add(JOINER_HOST)
    failed = leave()
    member = GroupMember.objects.get(instance=joiner)
    assert failed.status == OperationStatus.FAILED
    assert leave_failed(member)

    down.clear()
    succeeded(start(lambda: retry_leave(member, user=admin)))

    assert not GroupMember.objects.filter(instance=joiner).exists()
    config = AwgServerConfig.parse(server_files(servers[JOINER_HOST]).config)
    assert config.private_key != leader.private_key
    assert config.peers == []
    joiner.refresh_from_db()
    assert joiner.private_key == config.private_key


def test_server_kept_in_the_group_catches_up_with_its_keys(  # noqa: PLR0913, PLR0917
    servers: Fakes,
    leader: ProtocolInstance,
    joiner: ProtocolInstance,
    admin: User,
    start: Start,
    join: Run,
    leave: Run,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    succeeded(join())

    def unsaved(*args: object) -> None:
        raise RuntimeError("the panel database is gone")

    # The clean files are on the server, but the panel fails to save them.
    with monkeypatch.context() as patch:
        patch.setattr("apps.groups.services.cloning.take_identity", unsaved)
        assert leave().status == OperationStatus.FAILED
    assert not is_clone(servers)
    member = GroupMember.objects.get(instance=joiner)

    stay_in_group(member, user=admin)
    member.refresh_from_db()
    assert member.state == MemberState.MEMBER
    assert member.lagging_reason == LagReason.STAYED
    assert member not in backends(member.group)

    params = {"member_id": member.pk, "server_id": joiner.server_id}
    succeeded(start(lambda: launch(CATCH_UP_KIND, params, user=admin)))

    assert is_clone(servers)
    member.refresh_from_db()
    assert not member.lagging
    joiner.refresh_from_db()
    assert joiner.private_key == leader.private_key


def test_kept_server_starts_catching_up_at_once(  # noqa: PLR0913, PLR0917
    servers: Fakes,
    joiner: ProtocolInstance,
    admin: User,
    join: Run,
    leave: Run,
    django_capture_on_commit_callbacks: OnCommit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    succeeded(join())
    with monkeypatch.context() as patch:
        patch.setattr("apps.groups.services.cloning.take_identity", _unsaved)
        assert leave().status == OperationStatus.FAILED
    member = GroupMember.objects.get(instance=joiner)

    with django_capture_on_commit_callbacks(execute=True):
        stay_in_group(member, user=admin)

    succeeded(Operation.objects.get(kind=CATCH_UP_KIND))
    assert is_clone(servers)
    member.refresh_from_db()
    assert not member.lagging


def _unsaved(*args: object) -> None:
    raise RuntimeError("the panel database is gone")


def _failed_after_writing(join: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """The join writes the group files, but the panel fails to save them."""
    with monkeypatch.context() as patch:
        patch.setattr("apps.groups.services.cloning.take_identity", _unsaved)
        assert join().status == OperationStatus.FAILED


def test_failed_join_is_retried_to_a_clone(  # noqa: PLR0913, PLR0917
    servers: Fakes,
    leader: ProtocolInstance,
    joiner: ProtocolInstance,
    admin: User,
    down: set[str],
    start: Start,
    join: Run,
) -> None:
    down.add(JOINER_HOST)
    assert join().status == OperationStatus.FAILED
    member = GroupMember.objects.get(instance=joiner)
    assert join_failed(member)

    down.clear()
    succeeded(start(lambda: retry_join(member, user=admin)))

    assert is_clone(servers)
    member.refresh_from_db()
    assert member.state == MemberState.MEMBER
    joiner.refresh_from_db()
    assert joiner.private_key == leader.private_key


def test_join_failed_before_writing_is_undone_without_touching_the_server(  # noqa: PLR0913, PLR0917
    servers: Fakes, joiner: ProtocolInstance, admin: User, down: set[str], start: Start, join: Run
) -> None:
    before = server_files(servers[JOINER_HOST])
    down.add(JOINER_HOST)
    assert join().status == OperationStatus.FAILED
    member = GroupMember.objects.get(instance=joiner)

    down.clear()
    succeeded(start(lambda: cancel_join(member, user=admin)))

    assert not GroupMember.objects.filter(instance=joiner).exists()
    assert server_files(servers[JOINER_HOST]) == before
    assert not ProtocolSnapshot.objects.filter(reason=SnapshotReason.JOIN_CANCEL).exists()
    private_key = joiner.private_key
    joiner.refresh_from_db()
    assert joiner.private_key == private_key


def test_join_failed_after_writing_gets_back_the_snapshot(  # noqa: PLR0913, PLR0917
    servers: Fakes,
    joiner: ProtocolInstance,
    admin: User,
    start: Start,
    join: Run,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = server_files(servers[JOINER_HOST])
    _failed_after_writing(join, monkeypatch)
    assert is_clone(servers)
    group_files = server_files(servers[JOINER_HOST])
    member = GroupMember.objects.get(instance=joiner)

    succeeded(start(lambda: cancel_join(member, user=admin)))

    assert not GroupMember.objects.filter(instance=joiner).exists()
    assert server_files(servers[JOINER_HOST]) == before
    assert servers[JOINER_HOST].awg_up
    kept = ProtocolSnapshot.objects.get(instance=joiner, reason=SnapshotReason.JOIN_CANCEL)
    assert kept.config == group_files.config


def test_clean_server_whose_join_failed_gets_a_clean_amneziawg(  # noqa: PLR0913, PLR0917
    servers: Fakes,
    leader: ProtocolInstance,
    joiner: ProtocolInstance,
    admin: User,
    start: Start,
    join: Run,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean = ClientsTable([]).serialize().encode()
    servers[JOINER_HOST].container_files[AWG_CLIENTS_TABLE_PATH] = clean
    _failed_after_writing(join, monkeypatch)
    member = GroupMember.objects.get(instance=joiner)

    succeeded(start(lambda: cancel_join(member, user=admin)))

    assert not GroupMember.objects.filter(instance=joiner).exists()
    config = AwgServerConfig.parse(server_files(servers[JOINER_HOST]).config)
    assert config.private_key != leader.private_key
    assert config.peers == []
    joiner.refresh_from_db()
    assert joiner.private_key == config.private_key


def test_join_is_not_undone_on_a_server_with_a_foreign_key(  # noqa: PLR0913, PLR0917
    servers: Fakes,
    joiner: ProtocolInstance,
    admin: User,
    down: set[str],
    start: Start,
    join: Run,
) -> None:
    down.add(JOINER_HOST)
    assert join().status == OperationStatus.FAILED
    member = GroupMember.objects.get(instance=joiner)
    foreign = standalone_files(APP_CONFIG.address, joiner.port)
    servers[JOINER_HOST].container_files.update(foreign.by_path())
    before = server_files(servers[JOINER_HOST])
    down.clear()

    operation = start(lambda: cancel_join(member, user=admin))

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == "other_server_key"
    assert server_files(servers[JOINER_HOST]) == before
    assert GroupMember.objects.get(instance=joiner).state == MemberState.JOINING


def test_running_join_is_neither_retried_nor_undone(
    servers: Fakes, group: ServerGroup, joiner: ProtocolInstance, admin: User
) -> None:
    start_join(group, joiner, user=admin)
    member = GroupMember.objects.get(instance=joiner)

    assert not join_failed(member)
    for action in (retry_join, cancel_join):
        with pytest.raises(GroupError) as running:
            action(member, user=admin)
        assert running.value.code == GroupErrorCode.JOIN_RUNNING


def test_member_not_joining_cannot_be_taken_out(
    servers: Fakes, joiner: ProtocolInstance, admin: User, join: Run
) -> None:
    succeeded(join())
    member = GroupMember.objects.get(instance=joiner)

    assert not join_failed(member)
    with pytest.raises(GroupError) as error:
        cancel_join(member, user=admin)
    assert error.value.code == GroupErrorCode.NOT_JOINING


def test_running_leave_is_neither_retried_nor_undone(
    servers: Fakes, joiner: ProtocolInstance, admin: User, join: Run
) -> None:
    succeeded(join())
    member = GroupMember.objects.get(instance=joiner)
    start_leave(member, user=admin)
    member.refresh_from_db()

    assert not leave_failed(member)
    with pytest.raises(GroupError) as running:
        retry_leave(member, user=admin)
    assert running.value.code == GroupErrorCode.LEAVE_RUNNING
    with pytest.raises(GroupError):
        stay_in_group(member, user=admin)


def test_member_not_leaving_cannot_be_kept(
    servers: Fakes, joiner: ProtocolInstance, admin: User, join: Run
) -> None:
    succeeded(join())
    member = GroupMember.objects.get(instance=joiner)

    with pytest.raises(GroupError) as error:
        stay_in_group(member, user=admin)
    assert error.value.code == GroupErrorCode.NOT_LEAVING
