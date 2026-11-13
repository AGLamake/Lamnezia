"""Where the copies of the archived protocols are taken: hourly, before a removal, on restore."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from django.test import Client

from amnezia_core.services import TOR_CONTAINER
from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.backups import operations
from apps.backups.errors import RestoreError, RestoreErrorCode
from apps.backups.models import ProtocolBackup
from apps.backups.receivers import on_container_removing
from apps.backups.services import snapshot
from apps.backups.services.snapshot import save_all_backups
from apps.backups.tests.fake_containers import TarOpenVpnHost
from apps.backups.tests.test_archived import CASES, installed, log_of
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.models import ProtocolInstance
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.errors import ErrorCode, SshCommandError
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db


def removing(instance: ProtocolInstance, host: RemoteHost) -> str:
    operation = OperationFactory()
    ctx = OperationContext(operation)
    with ctx.step("Remove"):
        on_container_removing(ProtocolInstance, instance=instance, host=host, ctx=ctx)
    return log_of(operation)


def test_removal_takes_a_fresh_copy() -> None:
    instance, _protocol, fake = installed(CASES["openvpn"])

    log = removing(instance, fake.host())

    assert ProtocolBackup.objects.get(instance=instance).archive
    assert "A copy of the container files is saved in the panel." in log


def unreadable(monkeypatch: pytest.MonkeyPatch, protocol: object) -> None:
    def broken(*args: object) -> None:
        raise SshCommandError(ErrorCode.COMMAND_FAILED, "tar exit 2")

    monkeypatch.setattr(type(protocol), "read", broken)


def test_unreadable_container_keeps_the_last_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    instance, protocol, fake = installed(CASES["openvpn"])
    saved = protocol.save(instance, fake.host())
    assert saved is not None
    unreadable(monkeypatch, protocol)

    log = removing(instance, fake.host())

    assert ProtocolBackup.objects.get(instance=instance).archive == saved.archive
    assert "the last saved copy is kept" in log


def test_removal_without_any_copy_warns_about_the_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    instance, protocol, fake = installed(CASES["openvpn"])
    unreadable(monkeypatch, protocol)

    log = removing(instance, fake.host())

    assert not ProtocolBackup.objects.exists()
    assert "the keys of this protocol will not work again" in log


def test_hourly_copy_covers_the_archived_protocols(monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _protocol, fake = installed(CASES["wireguard"])

    @contextmanager
    def connect(target: Server) -> Iterator[RemoteHost]:
        yield fake.host()

    monkeypatch.setattr(snapshot, "connect", connect)

    save_all_backups()

    assert ProtocolBackup.objects.get().instance == instance


def test_restore_operation_puts_back_the_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    instance, protocol, old = installed(CASES["openvpn"])
    protocol.save(instance, old.host())
    new = TarOpenVpnHost()

    @contextmanager
    def connect(target: Server) -> Iterator[RemoteHost]:
        yield new.host()

    monkeypatch.setattr(operations, "connect", connect)
    ctx = OperationContext(OperationFactory(params={"instance_id": instance.pk}))

    operations.restore_protocol(ctx)

    assert new.openvpn_up and new.pki_runs == 0


def test_protocol_without_backups_is_not_restored() -> None:
    instance = ProtocolInstance.objects.create(
        server=ServerFactory(), container=TOR_CONTAINER, port=80
    )
    ctx = OperationContext(OperationFactory(params={"instance_id": instance.pk}))

    with pytest.raises(RestoreError) as error:
        operations.restore_protocol(ctx)

    assert error.value.code == RestoreErrorCode.NO_BACKUP


NO_BACKUP_WARNING = "The panel has no backup of this protocol yet."


def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def test_remove_dialog_warns_without_a_backup(client: Client) -> None:
    instance, protocol, fake = installed(CASES["openvpn"])
    url = f"/protocols/{instance.pk}/container/remove/"

    assert NO_BACKUP_WARNING in admin_client(client).get(url).content.decode()

    protocol.save(instance, fake.host())
    assert NO_BACKUP_WARNING not in client.get(url).content.decode()


def test_card_offers_the_restore_only_with_a_copy(client: Client) -> None:
    instance, protocol, fake = installed(CASES["xray"])
    ProtocolInstance.objects.create(server=instance.server, container=TOR_CONTAINER, port=80)
    card = f"/servers/{instance.server_id}/"
    restore = f"/backups/restore/{instance.pk}/"

    html = admin_client(client).get(card).content.decode()
    assert restore not in html
    assert "the panel saves it every hour" in html

    protocol.save(instance, fake.host())
    html = client.get(card).content.decode()
    assert restore in html
    assert html.count("/backups/restore/") == 1
