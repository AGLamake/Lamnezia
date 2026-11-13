"""Copies of the services with their own state: Tor, MTProxy, Telemt and SFTP."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from django.db import connection
from django.db.models import ProtectedError
from django.test import Client

from amnezia_core.proxies import MTPROXY_CONTAINER, MTPROXY_PORT, TELEMT_CONTAINER, TELEMT_PORT
from amnezia_core.proxy_options import TelegramProxyOptions
from amnezia_core.services import SFTP_CONTAINER, SFTP_PORT, TOR_CONTAINER, SftpAccount
from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.backups.errors import RestoreError, RestoreErrorCode
from apps.backups.models import ServiceBackup
from apps.backups.services import snapshot
from apps.backups.services.archive_restore import ServiceRestorer
from apps.backups.services.archived_services import ArchivedService, archived_services
from apps.backups.services.restore import RESTORE_SERVICE_KIND
from apps.backups.tests.fake_containers import TarServiceHost, onion
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.models import InstanceOrigin, ServiceInstance
from apps.protocols.services.installer import ContainerInstaller
from apps.protocols.services.proxy_installers import MtProxyInstaller, TelemtInstaller
from apps.protocols.services.service_installers import SftpInstaller, TorSiteInstaller
from apps.protocols.tests.fake_proxy_host import SECRET
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

OTHER_SECRET = "00112233445566778899aabbccddeeff"
SFTP_ACCOUNT = SftpAccount("sftp_user", "kept-password")


def tor(host: RemoteHost, ctx: OperationContext) -> ContainerInstaller[Any]:
    return TorSiteInstaller(host, ctx, port=80)


def mtproxy(host: RemoteHost, ctx: OperationContext) -> ContainerInstaller[Any]:
    return MtProxyInstaller(host, ctx, port=MTPROXY_PORT, options=TelegramProxyOptions())


def telemt(host: RemoteHost, ctx: OperationContext) -> ContainerInstaller[Any]:
    return TelemtInstaller(host, ctx, port=TELEMT_PORT, options=TelegramProxyOptions())


def sftp(host: RemoteHost, ctx: OperationContext) -> ContainerInstaller[Any]:
    return SftpInstaller(host, ctx, port=SFTP_PORT, account=SFTP_ACCOUNT)


def other_sftp(host: RemoteHost, ctx: OperationContext) -> ContainerInstaller[Any]:
    return SftpInstaller(host, ctx, port=SFTP_PORT, account=SftpAccount("sftp_user", "other"))


@dataclass(frozen=True)
class Case:
    container: str
    port: int
    install: Callable[[RemoteHost, OperationContext], ContainerInstaller[Any]]
    # What the panel keeps for the service installed on a host with `own_key` "a".
    login: str
    secret: str
    # The same service with other keys, on another server.
    rival: Callable[[RemoteHost, OperationContext], ContainerInstaller[Any]]


CASES = {
    "tor": Case(TOR_CONTAINER, 80, tor, "", onion("a"), tor),
    "mtproxy": Case(MTPROXY_CONTAINER, MTPROXY_PORT, mtproxy, "", SECRET, mtproxy),
    "telemt": Case(TELEMT_CONTAINER, TELEMT_PORT, telemt, "", SECRET, telemt),
    "sftp": Case(SFTP_CONTAINER, SFTP_PORT, sftp, SFTP_ACCOUNT.user, SFTP_ACCOUNT.password,
                 other_sftp),
}  # fmt: skip


def context() -> OperationContext:
    return OperationContext(OperationFactory())


def installed(case: Case) -> tuple[ServiceInstance, ArchivedService, TarServiceHost]:
    """The service installed by its own installer on a fake server, known to the panel."""
    fake = TarServiceHost(own_key="a")
    case.install(fake.host(), context()).run()
    service = ServiceInstance.objects.create(
        server=ServerFactory(),
        container=case.container,
        port=case.port,
        login=case.login,
        secret=case.secret,
        origin=InstanceOrigin.INSTALLED,
    )
    archived = archived_services.find(case.container)
    assert archived is not None
    return service, archived, fake


def rival_host(case: Case) -> TarServiceHost:
    """Another server where the same service runs with keys of its own, stopped."""
    fake = TarServiceHost(own_key="b", new_secret=OTHER_SECRET)
    case.rival(fake.host(), context()).run()
    fake.containers[case.container].state = "exited"
    return fake


def restore(service: ServiceInstance, archived: ArchivedService, fake: TarServiceHost) -> None:
    ServiceRestorer(service, fake.host(), context(), archived).run()


def identity_file(fake: TarServiceHost, archived: ArchivedService) -> bytes:
    state = archived.state
    return fake.container_files(state.container)[state.identity_path]


@pytest.mark.parametrize("name", CASES)
def test_restore_puts_the_service_keys_on_a_clean_server(name: str) -> None:
    service, archived, fake = installed(CASES[name])
    assert archived.save(service, fake.host()) is not None

    clean = TarServiceHost(own_key="c", new_secret=OTHER_SECRET)
    restore(service, archived, clean)

    assert identity_file(clean, archived) == identity_file(fake, archived)
    assert clean.containers[service.container].state == "running"
    if archived.makes_own_keys:
        # Tor and sshd made keys of their own at start: the copy replaced them.
        assert service.container in clean.restarted


@pytest.mark.parametrize("name", CASES)
def test_container_with_other_keys_is_not_started(name: str) -> None:
    case = CASES[name]
    service, archived, fake = installed(case)
    archived.save(service, fake.host())
    rival = rival_host(case)

    with pytest.raises(RestoreError) as error:
        restore(service, archived, rival)

    assert error.value.code == RestoreErrorCode.OTHER_CONTAINER
    assert rival.containers[case.container].state == "exited"
    assert case.container not in rival.seeded


@pytest.mark.parametrize("name", CASES)
def test_container_with_other_keys_keeps_the_last_copy(name: str) -> None:
    case = CASES[name]
    service, archived, fake = installed(case)
    archived.save(service, fake.host())
    taken_at = ServiceBackup.objects.get(service=service).taken_at
    rival = rival_host(case)
    rival.containers[case.container].state = "running"

    assert archived.save(service, rival.host()) is None
    assert ServiceBackup.objects.get(service=service).taken_at == taken_at


def test_restore_without_a_copy_is_refused() -> None:
    service, archived, _fake = installed(CASES["tor"])

    with pytest.raises(RestoreError) as error:
        restore(service, archived, TarServiceHost(own_key="c"))

    assert error.value.code == RestoreErrorCode.NO_BACKUP


def test_copy_is_stored_encrypted() -> None:
    service, archived, fake = installed(CASES["mtproxy"])
    backup = archived.save(service, fake.host())
    assert backup is not None

    with connection.cursor() as cursor:
        cursor.execute("SELECT archive FROM backups_servicebackup WHERE id = %s", [backup.pk])
        (stored,) = cursor.fetchone()
    assert stored != backup.archive


def test_hourly_copy_covers_the_services(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _archived, fake = installed(CASES["telemt"])

    @contextmanager
    def connect(target: Server) -> Iterator[RemoteHost]:
        assert target == service.server
        yield fake.host()

    monkeypatch.setattr(snapshot, "connect", connect)

    snapshot.save_all_backups()

    assert ServiceBackup.objects.get().service == service


def test_server_with_backups_is_not_deleted_silently() -> None:
    service, archived, fake = installed(CASES["sftp"])
    archived.save(service, fake.host())

    with pytest.raises(ProtectedError):
        service.server.delete()
    with pytest.raises(ProtectedError):
        Server.objects.filter(pk=service.server_id).delete()

    assert ServiceBackup.objects.filter(service=service).exists()


def test_removed_service_takes_its_copy_along() -> None:
    service, archived, fake = installed(CASES["sftp"])
    archived.save(service, fake.host())

    service.delete()
    service.server.delete()

    assert not ServiceBackup.objects.exists()


def test_server_without_backups_is_deleted() -> None:
    service, _archived, _fake = installed(CASES["tor"])

    service.server.delete()

    assert not ServiceInstance.objects.exists()


def test_restore_button_starts_the_service_operation(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))
    service, archived, fake = installed(CASES["tor"])
    archived.save(service, fake.host())

    html = client.get(f"/servers/{service.server_id}/").content.decode()
    assert f"/backups/restore-service/{service.pk}/" in html

    response = client.post(f"/backups/restore-service/{service.pk}/")

    assert response["Location"] == f"/servers/{service.server_id}/"
    operation = Operation.objects.get(kind=RESTORE_SERVICE_KIND)
    assert operation.kind == RESTORE_SERVICE_KIND
    assert operation.params == {"service_id": service.pk, "server_id": service.server_id}


def test_only_admins_restore_a_service(client: Client) -> None:
    client.force_login(UserFactory(role=Role.MODERATOR))
    service, _archived, _fake = installed(CASES["tor"])

    assert client.post(f"/backups/restore-service/{service.pk}/").status_code == 403
    assert not Operation.objects.filter(kind=RESTORE_SERVICE_KIND).exists()
