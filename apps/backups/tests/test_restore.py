from datetime import UTC, datetime
from ipaddress import IPv4Interface, IPv4Network

import pytest
from django.utils import timezone

from amnezia_core.awg import AwgServerConfig, Peer
from amnezia_core.awg_backup import interface_config
from amnezia_core.awg_install import AwgInstallSettings
from amnezia_core.awg_peers import AwgPeerRegistry
from amnezia_core.clients_table import ClientRecord, ClientsTable
from amnezia_core.keys import KeyPair
from apps.backups.errors import RestoreError, RestoreErrorCode
from apps.backups.models import ProtocolBackup
from apps.backups.services.restore import ProtocolRestorer, plan_restore
from apps.backups.tests.fake_containers import TarAwgServer as FakeAwgServer
from apps.backups.tests.helpers import PSK, access_key, awg_instance
from apps.keys.models import KeyStatus
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.awg_installer import AwgInstaller
from apps.protocols.tests.fake_awg import START_SCRIPT

pytestmark = pytest.mark.django_db

DIRECTORY = "/opt/amnezia/awg"
CONFIG = f"{DIRECTORY}/awg0.conf"
CLIENTS_TABLE = f"{DIRECTORY}/clientsTable"
APP_PEER = KeyPair.generate().public_key


def restore(server: FakeAwgServer, instance: ProtocolInstance) -> Operation:
    operation = OperationFactory()
    ProtocolRestorer(instance, server.host(), OperationContext(operation)).run()
    return operation


def log_of(operation: Operation) -> str:
    return "\n".join(operation.steps.values_list("log", flat=True))


def config_of(server: FakeAwgServer) -> AwgServerConfig:
    return AwgServerConfig.parse(server.container_files[CONFIG].decode())


def clients_of(server: FakeAwgServer) -> ClientsTable:
    return ClientsTable.parse(server.container_files[CLIENTS_TABLE].decode())


def backup_with_app_client(instance: ProtocolInstance, *peers: Peer) -> ProtocolBackup:
    """A copy taken when the server also had a client added in the Amnezia app."""
    config = interface_config(
        instance.private_key, IPv4Interface(instance.subnet), instance.port, instance.params
    )
    registry = AwgPeerRegistry(config, ClientsTable([]))
    added_at = datetime(2026, 1, 5, tzinfo=UTC)
    for peer in (Peer(APP_PEER, ("10.9.8.7/32",), PSK), *peers):
        registry.enable(peer, ClientRecord("from the app", added_at, None))
    return ProtocolBackup.objects.create(
        instance=instance,
        config=registry.config.serialize(),
        clients_table=registry.clients.serialize(),
        taken_at=timezone.now(),
    )


def test_new_server_gets_the_panel_keys_without_a_copy() -> None:
    instance = awg_instance()
    phone = access_key(instance, "Phone", "10.9.8.2")
    access_key(instance, "Laptop", "10.9.8.3", KeyStatus.DISABLED_MANUAL)
    access_key(instance, "Tablet", "10.9.8.4", KeyStatus.REVOKED)
    server = FakeAwgServer()

    operation = restore(server, instance)

    config = config_of(server)
    assert config.private_key == instance.private_key
    assert config.listen_port == instance.port
    assert config.address == IPv4Interface("10.9.8.0/24")
    assert config.awg_params == {name: value for name, value in instance.params.items() if value}
    assert config.peers == [Peer(phone.public_key, ("10.9.8.2/32",), PSK)]
    assert clients_of(server).name(phone.public_key) == "Phone"
    assert server.container_files[f"{DIRECTORY}/wireguard_psk.key"] == f"{PSK}\n".encode()
    assert server.container_files[f"{DIRECTORY}/wireguard_server_public_key.key"] == (
        f"{instance.public_key}\n".encode()
    )
    assert server.awg_up
    assert b"awg-quick up" in server.container_files[START_SCRIPT]
    assert "There is no copy of the server files" in log_of(operation)
    assert ProtocolBackup.objects.get(instance=instance).config == config.serialize()


def test_restored_interface_is_the_one_a_new_install_writes() -> None:
    instance = awg_instance()
    keys = KeyPair(instance.private_key, instance.public_key)
    installed = FakeAwgServer(server_keys=keys)
    settings = AwgInstallSettings(
        port=instance.port, subnet=IPv4Network(instance.subnet), params=instance.params
    )
    AwgInstaller(installed.host(), OperationContext(OperationFactory()), settings).run()
    restored = FakeAwgServer()

    restore(restored, instance)

    assert restored.container_files[CONFIG] == installed.container_files[CONFIG]


def test_copy_keeps_app_clients_and_follows_the_panel_keys() -> None:
    instance = awg_instance()
    disabled = access_key(instance, "Laptop", "10.9.8.3", KeyStatus.DISABLED_LIMIT)
    revoked = access_key(instance, "Tablet", "10.9.8.4", KeyStatus.REVOKED)
    backup_with_app_client(
        instance,
        Peer(disabled.public_key, ("10.9.8.3/32",), PSK),
        Peer(revoked.public_key, ("10.9.8.4/32",), PSK),
    )
    issued_later = access_key(instance, "Phone", "10.9.8.5")
    server = FakeAwgServer()

    operation = restore(server, instance)

    peers = {peer.public_key for peer in config_of(server).peers}
    assert peers == {APP_PEER, issued_later.public_key}
    clients = clients_of(server)
    assert clients.name(APP_PEER) == "from the app"
    assert disabled.public_key in clients
    assert revoked.public_key not in clients
    assert "Copy of the server files from" in log_of(operation)


def test_copy_of_another_server_key_is_not_used() -> None:
    instance = awg_instance()
    backup = backup_with_app_client(instance)
    backup.config = backup.config.replace(instance.private_key, KeyPair.generate().private_key)
    backup.save()

    plan = plan_restore(instance)

    assert plan.backup is None
    assert APP_PEER not in plan.files.config


def test_active_key_without_address_is_skipped() -> None:
    instance = awg_instance()
    access_key(instance, "Router", None)

    plan = plan_restore(instance)

    assert plan.skipped == ("Router",)
    assert plan.peers == 0


def test_issuing_key_is_left_to_its_own_operation() -> None:
    instance = awg_instance()
    access_key(instance, "Phone", "10.9.8.2", KeyStatus.ISSUING)

    assert plan_restore(instance).peers == 0


def test_restore_can_run_again() -> None:
    instance = awg_instance()
    phone = access_key(instance, "Phone", "10.9.8.2")
    server = FakeAwgServer()
    restore(server, instance)

    operation = restore(server, instance)

    assert config_of(server).private_key == instance.private_key
    assert [peer.public_key for peer in config_of(server).peers] == [phone.public_key]
    assert "awg0.conf already exists and is kept." in log_of(operation)


def test_server_with_another_awg_key_is_not_overwritten() -> None:
    instance = awg_instance()
    server = FakeAwgServer()
    AwgInstaller(server.host(), OperationContext(OperationFactory()), AwgInstallSettings()).run()
    before = dict(server.container_files)

    with pytest.raises(RestoreError) as caught:
        restore(server, instance)

    assert caught.value.code == RestoreErrorCode.OTHER_SERVER_KEY
    assert server.container_files == before
    assert not ProtocolBackup.objects.exists()
