from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from django.test import Client

from amnezia_core.awg import AwgServerConfig
from amnezia_core.keys import KeyPair
from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.backups.models import ProtocolBackup
from apps.backups.services import snapshot
from apps.backups.services.restore import RESTORE_KIND, ProtocolRestorer
from apps.backups.services.snapshot import save_all_backups, save_backup
from apps.backups.tests.helpers import awg_instance
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.fake_awg import FakeAwgServer
from apps.servers.models import Server
from apps.ssh.errors import ErrorCode, SshConnectionError
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

CONFIG = "/opt/amnezia/awg/awg0.conf"
CLIENTS_TABLE = "/opt/amnezia/awg/clientsTable"


def restored_server(instance: ProtocolInstance) -> FakeAwgServer:
    server = FakeAwgServer()
    ProtocolRestorer(instance, server.host(), OperationContext(OperationFactory())).run()
    return server


def test_copy_holds_the_server_files() -> None:
    instance = awg_instance()
    server = restored_server(instance)
    server.container_files[CLIENTS_TABLE] = b"[]"

    backup = save_backup(instance, server.host())

    assert backup is not None
    assert backup.config == server.container_files[CONFIG].decode()
    assert backup.clients_table == "[]"
    assert ProtocolBackup.objects.count() == 1


def test_server_with_another_key_keeps_the_old_copy() -> None:
    instance = awg_instance()
    server = restored_server(instance)
    old = ProtocolBackup.objects.get().config
    config = server.container_files[CONFIG].decode()
    other_key = KeyPair.generate().private_key
    server.container_files[CONFIG] = config.replace(instance.private_key, other_key).encode()

    assert save_backup(instance, server.host()) is None
    assert ProtocolBackup.objects.get().config == old
    assert AwgServerConfig.parse(old).private_key == instance.private_key


def test_unreachable_server_does_not_stop_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    broken, working = awg_instance(), awg_instance()
    server = restored_server(working)
    ProtocolBackup.objects.all().delete()

    @contextmanager
    def connect(target: Server) -> Iterator[RemoteHost]:
        if target == broken.server:
            raise SshConnectionError(ErrorCode.HOST_UNREACHABLE)
        yield server.host()

    monkeypatch.setattr(snapshot, "connect", connect)

    save_all_backups()

    assert list(ProtocolBackup.objects.values_list("instance", flat=True)) == [working.pk]


def test_restore_button_starts_the_operation(client: Client) -> None:
    admin = UserFactory(role=Role.ADMIN)
    client.force_login(admin)
    instance = awg_instance()

    response = client.post(f"/backups/restore/{instance.pk}/")

    assert response.status_code == 302
    assert response["Location"] == f"/servers/{instance.server_id}/"
    operation = Operation.objects.get()
    assert operation.kind == RESTORE_KIND
    assert operation.params == {"instance_id": instance.pk, "server_id": instance.server_id}
    event = AuditEvent.objects.get(action=RESTORE_KIND)
    assert event.actor == admin
    assert event.data == {"operation": operation.pk}


def test_only_admins_restore(client: Client) -> None:
    client.force_login(UserFactory(role=Role.MODERATOR))
    instance = awg_instance()

    assert client.post(f"/backups/restore/{instance.pk}/").status_code == 403
    assert not Operation.objects.exists()


def test_card_shows_the_copy_and_the_restore_button(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))
    instance = awg_instance()

    html = client.get(f"/servers/{instance.server_id}/").content.decode()
    assert "No copy of the files yet" in html
    assert f"/backups/restore/{instance.pk}/" in html

    restored_server(instance)
    html = client.get(f"/servers/{instance.server_id}/").content.decode()
    assert "Copy of the files from" in html
