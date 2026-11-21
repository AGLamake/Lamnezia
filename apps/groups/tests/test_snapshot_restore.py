import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from amnezia_core.awg import AWG_CLIENTS_TABLE_PATH, AWG_CONFIG_PATH, AwgServerConfig, Peer
from amnezia_core.awg_backup import AwgServerFiles
from amnezia_core.awg_peers import AwgPeerRegistry
from amnezia_core.clients_table import ClientRecord
from apps.accounts.models import User
from apps.backups.errors import RestoreError, RestoreErrorCode
from apps.backups.models import ProtocolSnapshot, SnapshotReason
from apps.backups.services.snapshot_restore import start_snapshot_restore
from apps.groups.models import GroupMember, ServerGroup
from apps.groups.services.membership import start_leave
from apps.groups.tests.conftest import JOINER_HOST, Fakes, Run, server_files, succeeded
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services import actions
from apps.operations.models import Operation, OperationStatus
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import PSK, Start, issued_key
from apps.protocols.tests.fake_awg import FakeAwgServer
from apps.relays.models import ServicePeer

pytestmark = pytest.mark.django_db


def snapshot_files(snapshot: ProtocolSnapshot) -> AwgServerFiles:
    return AwgServerFiles(snapshot.config, snapshot.clients_table, snapshot.preshared_key)


def peers_on(awg: FakeAwgServer) -> set[str]:
    config = AwgServerConfig.parse(server_files(awg).config)
    return {peer.public_key for peer in config.peers}


@pytest.fixture
def old_key(servers: Fakes, joiner: ProtocolInstance) -> AccessKey:
    """A key of the standalone server, with its peer in the server files."""
    key = issued_key(joiner)
    awg = servers[JOINER_HOST]
    files = awg.container_files
    registry = AwgPeerRegistry.parse(
        files[AWG_CONFIG_PATH].decode(), files[AWG_CLIENTS_TABLE_PATH].decode()
    )
    peer = Peer(key.public_key, (f"{key.client_ip}/32",), PSK)
    registry.enable(peer, ClientRecord(key.name, timezone.localtime(), None))
    files[AWG_CONFIG_PATH] = registry.config.serialize().encode()
    files[AWG_CLIENTS_TABLE_PATH] = registry.clients.serialize().encode()
    return key


@pytest.fixture
def left(
    old_key: AccessKey, joiner: ProtocolInstance, join: Run, admin: User, start: Start
) -> ProtocolSnapshot:
    """The snapshot taken when the server joined; the server has left the group since."""
    succeeded(join())
    succeeded(start(lambda: start_leave(GroupMember.objects.get(instance=joiner), user=admin)))
    return ProtocolSnapshot.objects.get(instance=joiner, reason=SnapshotReason.GROUP_JOIN)


@pytest.fixture
def restore(left: ProtocolSnapshot, admin: User, start: Start) -> Run:
    """Run the restore of the snapshot taken when the server joined."""
    return lambda: start(lambda: start_snapshot_restore(left, admin))


def test_server_gets_the_snapshot_files_and_old_keys_connect_again(
    servers: Fakes,
    joiner: ProtocolInstance,
    old_key: AccessKey,
    left: ProtocolSnapshot,
    restore: Run,
) -> None:
    old_key.refresh_from_db()
    assert old_key.status == KeyStatus.REVOKED

    succeeded(restore())

    assert server_files(servers[JOINER_HOST]) == snapshot_files(left)
    assert servers[JOINER_HOST].awg_up
    old_key.refresh_from_db()
    assert old_key.status == KeyStatus.ACTIVE
    assert old_key.revoked_at is None
    joiner.refresh_from_db()
    assert joiner.private_key in left.config
    assert joiner.preshared_key == left.preshared_key


def test_current_files_are_saved_before_the_restore(
    servers: Fakes, left: ProtocolSnapshot, restore: Run
) -> None:
    before = server_files(servers[JOINER_HOST])

    succeeded(restore())

    saved = ProtocolSnapshot.objects.get(reason=SnapshotReason.SNAPSHOT_RESTORE)
    assert snapshot_files(saved) == before


def test_keys_issued_after_leaving_are_revoked(
    servers: Fakes, joiner: ProtocolInstance, left: ProtocolSnapshot, restore: Run
) -> None:
    joiner.refresh_from_db()
    new_key = issued_key(joiner)

    succeeded(restore())

    new_key.refresh_from_db()
    assert new_key.status == KeyStatus.REVOKED


def test_interface_not_coming_up_brings_the_old_files_back(
    servers: Fakes, joiner: ProtocolInstance, left: ProtocolSnapshot, restore: Run
) -> None:
    before = server_files(servers[JOINER_HOST])
    joiner.refresh_from_db()
    key_before = joiner.private_key
    servers[JOINER_HOST].awg_refuses = str(AwgServerConfig.parse(left.config).private_key)

    operation = restore()

    assert operation.status == OperationStatus.FAILED
    assert server_files(servers[JOINER_HOST]) == before
    joiner.refresh_from_db()
    assert joiner.private_key == key_before


def test_restore_run_again_only_saves_the_result(
    servers: Fakes, left: ProtocolSnapshot, restore: Run
) -> None:
    succeeded(restore())

    succeeded(restore())

    assert ProtocolSnapshot.objects.filter(reason=SnapshotReason.SNAPSHOT_RESTORE).count() == 1


def test_member_of_a_group_is_refused(
    servers: Fakes, joiner: ProtocolInstance, old_key: AccessKey, join: Run, admin: User
) -> None:
    succeeded(join())
    snapshot = ProtocolSnapshot.objects.get(instance=joiner)

    with pytest.raises(RestoreError) as error:
        start_snapshot_restore(snapshot, admin)

    assert error.value.code == RestoreErrorCode.IN_GROUP
    assert not Operation.objects.filter(kind="backups.restore_snapshot").exists()


def test_view_shows_the_refusal_for_a_member(
    servers: Fakes, joiner: ProtocolInstance, old_key: AccessKey, join: Run, admin: User
) -> None:
    succeeded(join())
    snapshot = ProtocolSnapshot.objects.get(instance=joiner)
    client = Client()
    client.force_login(admin)

    response = client.post(reverse("backups:restore-snapshot", args=[snapshot.pk]), follow=True)

    assert "Take the server out of the group first" in response.content.decode()
    assert not Operation.objects.filter(kind="backups.restore_snapshot").exists()


def test_server_card_lists_the_snapshots(
    servers: Fakes, joiner: ProtocolInstance, left: ProtocolSnapshot, admin: User
) -> None:
    client = Client()
    client.force_login(admin)

    page = client.get(reverse("servers:detail", args=[joiner.server_id])).content.decode()

    assert reverse("backups:restore-snapshot", args=[left.pk]) in page


def test_server_card_shows_the_port_and_clients_of_a_snapshot(
    servers: Fakes, joiner: ProtocolInstance, left: ProtocolSnapshot, admin: User
) -> None:
    client = Client()
    client.force_login(admin)
    config = AwgServerConfig.parse(left.config)

    page = client.get(reverse("servers:detail", args=[joiner.server_id])).content.decode()

    assert f"port {config.listen_port}, {len(config.peers)} client" in page
    assert reverse("backups:delete-snapshot", args=[left.pk]) in page


def test_admin_deletes_a_snapshot(
    servers: Fakes, joiner: ProtocolInstance, left: ProtocolSnapshot, admin: User
) -> None:
    client = Client()
    client.force_login(admin)

    response = client.post(reverse("backups:delete-snapshot", args=[left.pk]))

    assert response.status_code == 302
    assert not ProtocolSnapshot.objects.filter(pk=left.pk).exists()


def test_peer_whose_key_lives_on_another_server_is_not_taken_twice(
    servers: Fakes,
    leader: ProtocolInstance,
    old_key: AccessKey,
    left: ProtocolSnapshot,
    restore: Run,
) -> None:
    AccessKey.objects.create(
        source=leader.source, name="iPhone", public_key=old_key.public_key, imported=True
    )

    succeeded(restore())

    old_key.refresh_from_db()
    assert old_key.status == KeyStatus.REVOKED
    assert AccessKey.objects.filter(public_key=old_key.public_key).count() == 2
    # Its client would connect here with the traffic not counted: the peer leaves the files.
    assert old_key.public_key not in peers_on(servers[JOINER_HOST])
    assert old_key.public_key not in server_files(servers[JOINER_HOST]).clients_table


@pytest.fixture
def probe(group: ServerGroup, old_key: AccessKey, left: ProtocolSnapshot) -> ServicePeer:
    """A service peer in the snapshot; the server has left the group, so only its key tells."""
    return ServicePeer.objects.create(
        group=group, private_key="probe", public_key=old_key.public_key, address="10.8.1.254"
    )


def test_a_service_peer_is_not_a_client_of_the_snapshot(
    joiner: ProtocolInstance, left: ProtocolSnapshot, probe: ServicePeer, admin: User
) -> None:
    config = AwgServerConfig.parse(left.config)
    client = Client()
    client.force_login(admin)

    page = client.get(reverse("servers:detail", args=[joiner.server_id])).content.decode()

    assert f"port {config.listen_port}, {len(config.peers) - 1} client" in page


def test_a_service_peer_is_not_restored(
    servers: Fakes, old_key: AccessKey, probe: ServicePeer, restore: Run
) -> None:
    succeeded(restore())

    assert probe.public_key not in peers_on(servers[JOINER_HOST])
    old_key.refresh_from_db()
    assert old_key.status == KeyStatus.REVOKED


def test_key_over_the_owner_plan_is_turned_off(
    servers: Fakes,
    leader: ProtocolInstance,
    old_key: AccessKey,
    left: ProtocolSnapshot,
    restore: Run,
) -> None:
    owner = UserLimitsFactory(plan=PlanFactory(max_keys=1), plan_assigned_at=timezone.now()).user
    AccessKey.objects.filter(pk=old_key.pk).update(user=owner)
    AccessKey.objects.create(user=owner, source=leader.source, name="Laptop", public_key="other")

    succeeded(restore())

    assert Operation.objects.filter(kind=actions.DISABLE, params__key_id=old_key.pk).exists()


def test_key_within_the_owner_plan_stays_on(
    servers: Fakes, old_key: AccessKey, left: ProtocolSnapshot, restore: Run
) -> None:
    owner = UserLimitsFactory(plan=PlanFactory(max_keys=2), plan_assigned_at=timezone.now()).user
    AccessKey.objects.filter(pk=old_key.pk).update(user=owner)

    succeeded(restore())

    assert not Operation.objects.filter(kind=actions.DISABLE).exists()


def test_server_card_warns_that_group_files_bring_the_group_key_back(
    servers: Fakes, joiner: ProtocolInstance, left: ProtocolSnapshot, admin: User
) -> None:
    ProtocolSnapshot.objects.filter(pk=left.pk).update(reason=SnapshotReason.JOIN_CANCEL)
    client = Client()
    client.force_login(admin)

    page = client.get(reverse("servers:detail", args=[joiner.server_id])).content.decode()

    assert "runs with the group key again" in page
