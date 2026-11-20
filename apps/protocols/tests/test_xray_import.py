"""A server with XRay set up in the Amnezia app is read into the panel with its clients."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from amnezia_core.clients_table import ClientsTable
from amnezia_core.xray import (
    XRAY,
    XRAY_CLIENTS_TABLE_PATH,
    XRAY_CONFIG_PATH,
    XRAY_OWN_CLIENT_PATH,
    XRAY_PUBLIC_KEY_PATH,
    XrayServerConfig,
    client_fingerprint,
)
from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import instances
from apps.keys.services.profile import XrayKeyProfile
from apps.keys.services.xray_profiles import follow_short_id, request_profiles
from apps.keys.tests.fake_xray import FakeXrayServer
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.services.importer import start_import
from apps.protocols.services.inventory import server_containers
from apps.protocols.tests.fake_metadata_host import amnezia_writes, without_metadata_block
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import CommandResult, RemoteHost
from apps.ssh.tests.fakes import result

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).resolve().parents[3] / "amnezia_core" / "tests" / "fixtures"
OWN = "6f0c3e52-1b7a-4c8e-9d2f-0a1b2c3d4e5f"
ANNA = "0d4f7a2b-8c3e-4f1a-9b6d-5e2c1a0f9d8e"
UNNAMED = "4b1e9c7d-2a3f-4e5b-8c6d-7f8091a2b3c4"
PUBLIC_KEY = "Z84J2IelR9ch3k8VtlVhhs5ycBUlXA7wHBWcBrjqnAw"
SHORT_ID = "a1b2c3d4e5f60718"
PRIVATE_KEY = "uGQ5Zl0n6xkVYPWk5aM4lJx2bS7cT8dE9fG0hI1jK2M"
DOCKER_PS = "amnezia-xray 0.0.0.0:443->443/tcp, 0.0.0.0:443->443/udp\n"
OnCommit = Callable[..., AbstractContextManager[Any]]


class XrayAppServer(FakeXrayServer):
    """XRay set up in the Amnezia app, answering `docker ps` too."""

    def handle(self, command: str, stdin: bytes) -> CommandResult:
        if "docker ps" in command:
            self.commands.append(command)
            return result(stdout=DOCKER_PS.encode())
        return super().handle(command, stdin)


def app_files() -> dict[str, str]:
    config = XrayServerConfig.parse((FIXTURES / "xray_server.json").read_text(encoding="utf-8"))
    config.add_client(ANNA, "xtls-rprx-vision")
    config.add_client(UNNAMED, "xtls-rprx-vision")
    clients = ClientsTable([])
    clients.add(ANNA, "Телефон Анны", datetime(2026, 1, 5, 12, 0))
    return {
        XRAY_CONFIG_PATH: config.serialize(),
        XRAY_CLIENTS_TABLE_PATH: clients.serialize(),
        XRAY_OWN_CLIENT_PATH: f"{OWN}\n",
        XRAY_PUBLIC_KEY_PATH: f"{PUBLIC_KEY}\n",
    }


@pytest.fixture
def app_server(monkeypatch: pytest.MonkeyPatch) -> XrayAppServer:
    server = XrayAppServer(app_files())

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield server.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    monkeypatch.setattr("apps.protocols.providers.connect", connect)
    return server


def run_import(server: Server, django_capture_on_commit_callbacks: OnCommit) -> Operation:
    with django_capture_on_commit_callbacks(execute=True):
        operation = start_import(server, None)
    operation.refresh_from_db()
    return operation


@pytest.fixture
def imported(app_server: XrayAppServer, django_capture_on_commit_callbacks: OnCommit) -> Server:
    server = ServerFactory(host="203.0.113.10")
    operation = run_import(server, django_capture_on_commit_callbacks)
    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    return server


def test_import_records_xray_with_its_reality_settings(imported: Server) -> None:
    instance = ProtocolInstance.objects.get()

    assert (instance.server, instance.container, instance.port) == (imported, "amnezia-xray", 443)
    assert instance.origin == InstanceOrigin.IMPORTED
    assert instance.params == {
        "site": "www.googletagmanager.com",
        "sni": "www.googletagmanager.com",
        "flow": "xtls-rprx-vision",
        "fingerprint": "chrome",
        "short_id": SHORT_ID,
    }
    assert (instance.public_key, instance.private_key) == (PUBLIC_KEY, PRIVATE_KEY)
    assert instance.title == "XRay (VLESS Reality)"


def test_clients_become_ownerless_keys_without_the_uuid_in_the_open(imported: Server) -> None:
    keys = {key.private_key: key for key in AccessKey.objects.all()}

    assert set(keys) == {OWN, ANNA, UNNAMED}
    anna = keys[ANNA]
    assert (anna.name, anna.status, anna.user, anna.client_ip) == (
        "Телефон Анны",
        KeyStatus.ACTIVE,
        None,
        None,
    )
    assert anna.public_key == client_fingerprint(ANNA)
    assert not anna.can_export
    assert anna.awaits_profile
    assert keys[UNNAMED].name == "Imported client 3"  # third in server.json


def test_the_client_of_the_app_that_installed_the_server_is_adopted(imported: Server) -> None:
    own = AccessKey.objects.get(public_key=client_fingerprint(OWN))

    assert (own.name, own.status, own.user) == ("Installer's client", KeyStatus.ACTIVE, None)


def test_imported_keys_get_their_profile_when_shown(
    imported: Server, django_capture_on_commit_callbacks: OnCommit
) -> None:
    keys = list(AccessKey.objects.all())

    with django_capture_on_commit_callbacks(execute=True):
        assert request_profiles(keys)

    for key in AccessKey.objects.all():
        assert key.can_export
        assert XrayKeyProfile.from_json(key.profile).short_id == SHORT_ID


def test_a_new_short_id_is_remembered_by_the_instance(imported: Server) -> None:
    instance = ProtocolInstance.objects.get()

    follow_short_id(instance.source, "0011223344556677")

    instance.refresh_from_db()
    assert instance.params["short_id"] == "0011223344556677"
    assert instances.get(instance.source).short_id == "0011223344556677"


def shown(django_capture_on_commit_callbacks: OnCommit) -> list[AccessKey]:
    """Every imported key with its profile built, as after the owners added them to the app."""
    with django_capture_on_commit_callbacks(execute=True):
        request_profiles(AccessKey.objects.all())
    return list(AccessKey.objects.order_by("pk"))


def test_owners_of_keys_with_the_old_short_id_are_told_to_import_them_again(
    imported: Server, django_capture_on_commit_callbacks: OnCommit
) -> None:
    anna, bob = UserFactory(), UserFactory()
    first, second, ownerless = shown(django_capture_on_commit_callbacks)
    AccessKey.objects.filter(pk__in=[first.pk, second.pk]).update(user=anna)
    AccessKey.objects.filter(pk=ownerless.pk).update(user=None)
    AccessKey.objects.create(source=first.source, name="New", public_key="x", user=bob)
    source = ProtocolInstance.objects.get().source

    with django_capture_on_commit_callbacks(execute=True):
        assert follow_short_id(source, "0011223344556677") == 3

    told = Notification.objects.filter(event=Event.SHORT_ID_CHANGED)
    assert [(n.user, n.data["server"]) for n in told] == [(anna, imported.name)]


def test_nobody_is_told_while_the_short_id_stays(
    imported: Server, django_capture_on_commit_callbacks: OnCommit
) -> None:
    key, *_ = shown(django_capture_on_commit_callbacks)
    AccessKey.objects.filter(pk=key.pk).update(user=UserFactory())

    with django_capture_on_commit_callbacks(execute=True):
        assert follow_short_id(key.source, SHORT_ID) == 0

    assert not Notification.objects.filter(event=Event.SHORT_ID_CHANGED).exists()


@pytest.fixture
def admin_client(admin: User) -> Client:
    client = Client()
    client.force_login(admin)
    return client


def test_card_offers_to_prepare_all_imported_keys(imported: Server, admin_client: Client) -> None:
    instance = ProtocolInstance.objects.get()

    [row] = server_containers(imported)
    response = admin_client.get(reverse("servers:detail", args=[imported.pk]))

    assert row.xray_waiting == 3
    assert reverse("protocols:xray-profiles", args=[instance.pk]) in response.content.decode()


def test_one_operation_prepares_all_imported_keys(
    imported: Server, admin_client: Client, django_capture_on_commit_callbacks: OnCommit
) -> None:
    url = reverse("protocols:xray-profiles", args=[ProtocolInstance.objects.get().pk])

    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(url)

    operation = Operation.objects.get(kind="keys.build_xray_profiles")
    assert response.headers["Location"] == reverse("operations:detail", args=[operation.pk])
    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert all(key.can_export for key in AccessKey.objects.all())
    [row] = server_containers(imported)
    assert row.xray_waiting == 0


def test_a_build_under_way_is_not_started_twice(imported: Server, admin_client: Client) -> None:
    url = reverse("protocols:xray-profiles", args=[ProtocolInstance.objects.get().pk])

    first, second = admin_client.post(url), admin_client.post(url)

    assert first.headers["Location"] == second.headers["Location"]
    assert Operation.objects.filter(kind="keys.build_xray_profiles").count() == 1


def test_import_changes_nothing_on_the_server(app_server: XrayAppServer, imported: Server) -> None:
    # The operation started after the import closes the cloud metadata service on its own.
    assert without_metadata_block(app_server.files) == app_files()
    assert app_server.restarts == 0
    assert not amnezia_writes(app_server.commands)


def test_import_again_adds_only_new_clients(
    app_server: XrayAppServer, imported: Server, django_capture_on_commit_callbacks: OnCommit
) -> None:
    AccessKey.objects.filter(public_key=client_fingerprint(ANNA)).update(
        name="Renamed in the panel"
    )
    config = XrayServerConfig.parse(app_server.files[XRAY_CONFIG_PATH])
    config.add_client("9e8d7c6b-5a49-4382-a1b0-c9d8e7f6a5b4", "xtls-rprx-vision")
    app_server.files[XRAY_CONFIG_PATH] = config.serialize()

    run_import(imported, django_capture_on_commit_callbacks)

    assert ProtocolInstance.objects.count() == 1
    assert AccessKey.objects.count() == 4
    assert AccessKey.objects.get(public_key=client_fingerprint(ANNA)).name == "Renamed in the panel"


def test_server_without_the_reality_public_key_fails_with_a_reason(
    app_server: XrayAppServer, django_capture_on_commit_callbacks: OnCommit
) -> None:
    del app_server.files[XRAY_PUBLIC_KEY_PATH]

    operation = run_import(ServerFactory(), django_capture_on_commit_callbacks)

    assert operation.status == OperationStatus.FAILED
    assert not AccessKey.objects.exists()


def test_xray_over_another_transport_is_not_imported(
    app_server: XrayAppServer, django_capture_on_commit_callbacks: OnCommit
) -> None:
    app_server.files[XRAY_CONFIG_PATH] = app_server.files[XRAY_CONFIG_PATH].replace(
        '"network": "tcp"', '"network": "ws"'
    )

    operation = run_import(ServerFactory(), django_capture_on_commit_callbacks)

    assert operation.status == OperationStatus.FAILED
    assert not ProtocolInstance.objects.exists()


def test_card_counts_the_xray_keys(imported: Server) -> None:
    imported.refresh_from_db()

    rows = server_containers(imported)

    assert [(row.name, row.port, row.supported, row.keys) for row in rows] == [
        ("amnezia-xray", 443, True, 3)
    ]


def test_keys_can_be_issued_on_the_imported_xray(imported: Server) -> None:
    instance = ProtocolInstance.objects.get()

    target = instances.get(instance.source)

    assert target.container is XRAY
    assert target.host == "203.0.113.10"
