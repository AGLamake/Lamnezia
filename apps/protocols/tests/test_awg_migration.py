from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from django.test import Client
from django.urls import reverse

from amnezia_core.awg import AWG_CLIENTS_TABLE_PATH, AwgServerConfig
from amnezia_core.awg_export import has_awg3_params
from amnezia_core.clients_table import ClientsTable
from amnezia_core.containers import AWG2, AWG_LEGACY
from amnezia_core.keys import KeyPair
from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.profile import ServerProfile
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.operations.models import Operation, OperationStatus
from apps.protocols.errors import SetupErrorCode
from apps.protocols.models import AwgMigration, InstanceOrigin, ProtocolInstance
from apps.protocols.services.awg_migration import (
    FINISH_KIND,
    MIGRATE_KIND,
    MigrationState,
    start_finish,
    start_migration,
)
from apps.protocols.tests.awg_case import APP_CONFIG, PSK, Start, issued_key
from apps.protocols.tests.fake_awg import START_SCRIPT, FakeAwgServer
from apps.protocols.tests.test_awg_upgrade import secrets_written
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

LEGACY_PORT = 55424
NEW_PORT = 40001


def serve(monkeypatch: pytest.MonkeyPatch, fake: FakeAwgServer) -> FakeAwgServer:
    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield fake.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    return fake


@pytest.fixture
def legacy() -> ProtocolInstance:
    return legacy_on(ServerFactory(host="203.0.113.10"))


def legacy_on(server: Server, port: int = LEGACY_PORT) -> ProtocolInstance:
    return ProtocolInstance.objects.create(
        server=server,
        container=AWG_LEGACY.name,
        port=port,
        subnet="10.8.1.0/24",
        params=APP_CONFIG.awg_params,
        public_key=KeyPair.generate().public_key,
        private_key=KeyPair.generate().private_key,
        preshared_key=PSK,
        origin=InstanceOrigin.IMPORTED,
    )


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> FakeAwgServer:
    """A server with no amnezia-awg2: the move installs it."""
    return serve(monkeypatch, FakeAwgServer())


def legacy_key(legacy: ProtocolInstance, owner: User | None, status: KeyStatus) -> AccessKey:
    key = issued_key(legacy, owner)
    key.status = status
    key.save(update_fields=["status"])
    return key


def moved(legacy: ProtocolInstance) -> dict[int, AccessKey]:
    """New keys by the id of the old key they replace."""
    pairs = AwgMigration.objects.get(legacy=legacy).pairs
    keys = AccessKey.objects.in_bulk([new for _old, new in pairs])
    return {old: keys[new] for old, new in pairs}


def target(legacy: ProtocolInstance) -> ProtocolInstance:
    return ProtocolInstance.objects.get(server=legacy.server, container=AWG2.name)


def test_clients_get_keys_on_a_new_awg3_container(
    fresh: FakeAwgServer, legacy: ProtocolInstance, admin: User, start: Start
) -> None:
    anna, bob = UserFactory(email="anna@example.com"), UserFactory(email="bob@example.com")
    active = legacy_key(legacy, anna, KeyStatus.ACTIVE)
    disabled = legacy_key(legacy, bob, KeyStatus.DISABLED_MANUAL)
    imported = legacy_key(legacy, None, KeyStatus.ACTIVE)
    revoked = legacy_key(legacy, anna, KeyStatus.REVOKED)

    operation = start(lambda: start_migration(legacy, admin, port=NEW_PORT))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert operation.kind == MIGRATE_KIND
    assert fresh.published_port == NEW_PORT
    new = target(legacy)
    assert new.origin == InstanceOrigin.INSTALLED
    assert not new.awg_outdated
    keys = moved(legacy)
    assert set(keys) == {active.pk, disabled.pk, imported.pk}
    assert keys[active.pk].user == anna
    assert keys[active.pk].name == active.name
    assert keys[active.pk].status == KeyStatus.ACTIVE
    assert keys[imported.pk].status == KeyStatus.ACTIVE
    assert keys[disabled.pk].status == KeyStatus.DISABLED_MANUAL
    for key in keys.values():
        assert key.source == new.source
        assert key.client_ip
        assert has_awg3_params(ServerProfile.from_json(key.profile).params)
    config = AwgServerConfig.parse(fresh.container_files[AWG2.config_path].decode())
    on_server = {peer.public_key for peer in config.peers}
    assert on_server == {keys[active.pk].public_key, keys[imported.pk].public_key}
    clients = ClientsTable.parse(fresh.container_files[AWG_CLIENTS_TABLE_PATH].decode())
    assert keys[active.pk].public_key in clients.serialize()
    for old in (active, disabled, imported, revoked):
        status = old.status
        old.refresh_from_db()
        assert old.status == status, "the old container keeps working"
    assert ProtocolInstance.objects.filter(pk=legacy.pk).exists()
    assert {n.user for n in Notification.objects.filter(event=Event.KEYS_MOVED)} == {anna, bob}
    state = MigrationState.of(legacy)
    assert state.migration is not None and state.migration.moved_at is not None
    assert state.can_finish
    assert secrets_written(operation, new.params["HeaderProtectionKey"]) == []


def test_run_again_moves_only_keys_issued_since(
    fresh: FakeAwgServer, legacy: ProtocolInstance, admin: User, start: Start
) -> None:
    first = legacy_key(legacy, UserFactory(), KeyStatus.ACTIVE)
    start(lambda: start_migration(legacy, admin, port=NEW_PORT))
    later = legacy_key(legacy, UserFactory(), KeyStatus.ACTIVE)
    assert not MigrationState.of(legacy).can_finish

    operation = start(lambda: start_migration(legacy, admin, port=NEW_PORT))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    keys = moved(legacy)
    assert set(keys) == {first.pk, later.pk}
    assert AccessKey.objects.filter(source=target(legacy).source).count() == 2
    assert MigrationState.of(legacy).can_finish


def test_outdated_awg2_blocks_the_move(
    connected: FakeAwgServer,
    instance: ProtocolInstance,
    admin: User,
    start: Start,
) -> None:
    """The server has amnezia-awg2 on 2.0: it must be moved to 3.1 first."""
    legacy = legacy_on(instance.server, port=55425)
    legacy_key(legacy, UserFactory(), KeyStatus.ACTIVE)

    operation = start(lambda: start_migration(legacy, admin, port=NEW_PORT))

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == SetupErrorCode.MIGRATION_TARGET_OUTDATED
    assert not AccessKey.objects.filter(source=instance.source).exists()
    assert MigrationState.of(legacy).blocked


@pytest.fixture
def moved_legacy(
    fresh: FakeAwgServer, legacy: ProtocolInstance, admin: User, start: Start
) -> ProtocolInstance:
    legacy_key(legacy, UserFactory(email="anna@example.com"), KeyStatus.ACTIVE)
    operation = start(lambda: start_migration(legacy, admin, port=NEW_PORT))
    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    Notification.objects.all().delete()
    return legacy


@pytest.fixture
def legacy_server(monkeypatch: pytest.MonkeyPatch) -> FakeAwgServer:
    """The old container, running: finishing the move removes it."""
    return serve(
        monkeypatch,
        FakeAwgServer(
            kind=AWG_LEGACY,
            container_state="running",
            container_files={START_SCRIPT: b"#!/bin/bash\n"},
            awg_up=True,
            published_port=LEGACY_PORT,
        ),
    )


def test_finish_removes_the_old_container_and_revokes_its_keys(
    moved_legacy: ProtocolInstance, legacy_server: FakeAwgServer, admin: User, start: Start
) -> None:
    pairs = moved(moved_legacy)
    source = moved_legacy.source

    operation = start(lambda: start_finish(moved_legacy, admin))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert operation.kind == FINISH_KIND
    assert legacy_server.container_state == ""
    assert not ProtocolInstance.objects.filter(pk=moved_legacy.pk).exists()
    assert not AwgMigration.objects.exists()
    for old in AccessKey.objects.filter(source=source):
        assert old.status == KeyStatus.REVOKED
        assert old.private_key == old.profile == ""
    for new in pairs.values():
        new.refresh_from_db()
        assert new.status == KeyStatus.ACTIVE
    assert not Notification.objects.filter(event=Event.KEYS_REVOKED).exists()


def test_finish_is_refused_while_a_key_is_not_moved(
    moved_legacy: ProtocolInstance, legacy_server: FakeAwgServer, admin: User, start: Start
) -> None:
    legacy_key(moved_legacy, UserFactory(), KeyStatus.ACTIVE)

    operation = start(lambda: start_finish(moved_legacy, admin))

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == SetupErrorCode.MIGRATION_NOT_DONE
    assert legacy_server.container_state == "running"
    assert ProtocolInstance.objects.filter(pk=moved_legacy.pk).exists()


@pytest.fixture
def admin_client(admin: User) -> Client:
    client = Client()
    client.force_login(admin)
    return client


def test_server_card_offers_the_move(admin_client: Client, legacy: ProtocolInstance) -> None:
    response = admin_client.get(reverse("servers:detail", args=[legacy.server_id]))

    assert response.status_code == 200
    assert reverse("protocols:awg-migration", args=[legacy.pk]) in response.content.decode()
    assert reverse("protocols:awg-migration-finish", args=[legacy.pk]) not in (
        response.content.decode()
    )


def test_move_needs_a_free_port(admin_client: Client, legacy: ProtocolInstance) -> None:
    url = reverse("protocols:awg-migration", args=[legacy.pk])

    response = admin_client.post(url, {"port": LEGACY_PORT})

    assert response.status_code == 302
    assert not Operation.objects.exists()


def test_finish_without_a_move_is_not_found(admin_client: Client, legacy: ProtocolInstance) -> None:
    url = reverse("protocols:awg-migration-finish", args=[legacy.pk])

    assert admin_client.post(url).status_code == 404
    assert not Operation.objects.exists()
