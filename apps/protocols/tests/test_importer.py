from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import pytest
from django.test import Client

from amnezia_core.awg import AWG_CLIENTS_TABLE_PATH, AWG_CONFIG_PATH, AWG_PSK_PATH
from amnezia_core.clients_table import ClientsTable
from amnezia_core.containers import AWG2, AWG_LEGACY, WIREGUARD, WgContainer
from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.actions import KeyActions
from apps.keys.services.instances import instances
from apps.keys.tests.fake_server import FakeAwgServer
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import CONTAINER_TITLES, InstanceOrigin, ProtocolInstance
from apps.protocols.providers import traffic_targets
from apps.protocols.services.importer import IMPORT_KIND, start_import
from apps.protocols.services.inventory import server_containers
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import CommandResult, RemoteHost
from apps.ssh.tests.fakes import result

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).resolve().parents[3] / "amnezia_core" / "tests" / "fixtures"
PSK = "u+xLktHw90auwUVtjZEJVpNCbfbFK8GNmGegY/Z0XYs="
ANNA = "jhkQdvDcgDYudvey3hItwkaQoDy/bLT6QnsyYHwCIlo="
UNNAMED = "Q2+/5jIlpVjLMKDArTagVYSPsI5fcqPNpSem9NMi50s="
DOCKER_PS = (
    "amnezia-awg2 0.0.0.0:55424->55424/udp, [::]:55424->55424/udp\n"
    "amnezia-openvpn-cloak 0.0.0.0:443->443/tcp\n"
)
OnCommit = Callable[..., AbstractContextManager[Any]]


class AppServer(FakeAwgServer):
    """A server set up by the Amnezia app, answering `docker ps` too."""

    def __init__(
        self, files: dict[str, str], docker_ps: str = DOCKER_PS, kind: WgContainer = AWG2
    ) -> None:
        super().__init__(files, kind=kind)
        self.docker_ps = docker_ps

    def handle(self, command: str, stdin: bytes) -> CommandResult:
        if "docker ps" in command:
            self.commands.append(command)
            return result(stdout=self.docker_ps.encode())
        return super().handle(command, stdin)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def app_server() -> AppServer:
    return AppServer(
        {
            AWG_CONFIG_PATH: fixture("awg0.conf"),
            AWG_CLIENTS_TABLE_PATH: fixture("clientsTable.json"),
            AWG_PSK_PATH: f"{PSK}\n",
        }
    )


@pytest.fixture
def connected(app_server: AppServer, monkeypatch: pytest.MonkeyPatch) -> AppServer:
    """Every connection of the panel, from operations and providers, reaches `app_server`."""

    @contextmanager
    def connect(server: Server) -> Iterator[RemoteHost]:
        yield app_server.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    monkeypatch.setattr("apps.protocols.providers.connect", connect)
    return app_server


@pytest.fixture
def run(django_capture_on_commit_callbacks: OnCommit) -> Callable[[Callable[[], Operation]], None]:
    """Start an operation and run it at once, as the worker would."""

    def runner(start: Callable[[], Operation]) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            operation = start()
        operation.refresh_from_db()
        assert operation.status == OperationStatus.SUCCEEDED, operation.error_message

    return runner


@pytest.fixture
def imported(connected: AppServer, run: Callable[[Callable[[], Operation]], None]) -> Server:
    server = ServerFactory(host="203.0.113.10")
    run(lambda: start_import(server, None))
    return server


def test_import_records_the_protocol_with_its_settings(imported: Server) -> None:
    instance = ProtocolInstance.objects.get()

    assert instance.server == imported
    assert (instance.container, instance.port, instance.subnet) == (
        "amnezia-awg2",
        55424,
        "10.8.1.0/24",
    )
    assert instance.origin == InstanceOrigin.IMPORTED
    assert instance.params["Jc"] == "4"
    assert instance.params["H1"] == "1020325451-1025713284"
    assert instance.preshared_key == PSK
    assert instance.public_key


def test_every_peer_becomes_an_ownerless_key(imported: Server) -> None:
    keys = {key.public_key: key for key in AccessKey.objects.all()}

    assert set(keys) == {ANNA, UNNAMED}
    anna = keys[ANNA]
    assert (anna.name, anna.client_ip, anna.status) == (
        "Ноутбук Анны",
        "10.8.1.1",
        KeyStatus.ACTIVE,
    )
    assert anna.user is None
    assert anna.private_key == ""
    assert anna.imported
    assert not anna.can_export
    assert keys[UNNAMED].name == "Client 1"


def test_import_changes_nothing_on_the_server(app_server: AppServer, imported: Server) -> None:
    files_before = {
        AWG_CONFIG_PATH: fixture("awg0.conf"),
        AWG_CLIENTS_TABLE_PATH: fixture("clientsTable.json"),
        AWG_PSK_PATH: f"{PSK}\n",
    }

    assert app_server.files == files_before
    assert not [c for c in app_server.commands if "target=" in c or "syncconf" in c]
    assert not app_server.lock.locked()


def test_import_again_adds_only_new_clients(
    app_server: AppServer, imported: Server, run: Callable[[Callable[[], Operation]], None]
) -> None:
    AccessKey.objects.filter(public_key=ANNA).update(name="Renamed in the panel")
    app_server.files[AWG_CONFIG_PATH] += (
        "\n[Peer]\nPublicKey = 3Zx7Q2bBVZc/Zx3x3oI8yQ1u9bKgk1PZ4w7Gm3kq9Ew=\n"
        "AllowedIPs = 10.8.1.3/32\n"
    )

    run(lambda: start_import(imported, None))

    assert ProtocolInstance.objects.count() == 1
    assert AccessKey.objects.count() == 3
    assert AccessKey.objects.get(public_key=ANNA).name == "Renamed in the panel"
    assert AccessKey.objects.get(client_ip="10.8.1.3").name == "Imported client 3"


def test_legacy_clients_table_names_the_clients(
    app_server: AppServer, connected: AppServer, run: Callable[[Callable[[], Operation]], None]
) -> None:
    app_server.files[AWG_CLIENTS_TABLE_PATH] = fixture("clientsTable_legacy.json")
    server = ServerFactory()

    run(lambda: start_import(server, None))

    assert AccessKey.objects.get(public_key=ANNA).name == "Phone"


def test_other_containers_are_listed_as_not_supported(imported: Server) -> None:
    imported.refresh_from_db()

    rows = server_containers(imported)

    assert [(row.name, row.port, row.supported, row.keys) for row in rows] == [
        ("amnezia-awg2", 55424, True, 2),
        ("amnezia-openvpn-cloak", 443, False, 0),
    ]
    assert rows[0].instance == ProtocolInstance.objects.get()


def test_server_without_supported_containers_imports_nothing(
    app_server: AppServer, connected: AppServer, run: Callable[[Callable[[], Operation]], None]
) -> None:
    app_server.docker_ps = "amnezia-openvpn-cloak 0.0.0.0:443->443/tcp\n"
    server = ServerFactory()

    run(lambda: start_import(server, None))

    server.refresh_from_db()
    assert not ProtocolInstance.objects.exists()
    assert server.containers == [{"name": "amnezia-openvpn-cloak", "port": 443, "transport": "tcp"}]


def test_unreadable_config_fails_with_a_reason(
    app_server: AppServer,
    connected: AppServer,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    app_server.files[AWG_CONFIG_PATH] = "[Interface]\nAddress = 10.8.1.0/24\n"
    server = ServerFactory()

    with django_capture_on_commit_callbacks(execute=True):
        operation = start_import(server, None)

    operation.refresh_from_db()
    assert operation.status == OperationStatus.FAILED
    assert operation.error_message == "The protocol configuration on the server cannot be read."
    assert not ProtocolInstance.objects.exists()


def test_imported_instance_is_offered_for_keys_and_traffic(imported: Server) -> None:
    instance = ProtocolInstance.objects.get()

    offered = instances.get(instance.source)
    [target] = traffic_targets()

    assert offered.host == "203.0.113.10"
    assert offered.title == f"{imported.name} · AmneziaWG 2.0"
    assert target.containers == {instance.source: "amnezia-awg2"}


def test_key_issued_after_import_keeps_the_app_clients(
    app_server: AppServer, imported: Server, run: Callable[[Callable[[], Operation]], None]
) -> None:
    source = ProtocolInstance.objects.get().source

    run(lambda: KeyActions(UserFactory(role=Role.ADMIN)).issue(source, "iPhone", None))

    clients = ClientsTable.parse(app_server.files[AWG_CLIENTS_TABLE_PATH])
    assert {ANNA, UNNAMED} <= set(clients.client_ids)
    assert clients.name(ANNA) == "Ноутбук Анны"
    assert len(clients) == 3


def test_import_button_starts_the_import(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))
    server = ServerFactory()

    response = client.post(f"/protocols/import/{server.pk}/")

    assert response.status_code == 302
    assert response["Location"] == f"/servers/{server.pk}/"
    assert Operation.objects.get().kind == IMPORT_KIND


def test_card_shows_imported_and_unsupported_containers(client: Client, imported: Server) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))

    html = client.get(f"/servers/{imported.pk}/").content.decode()

    assert "AmneziaWG 2.0" in html
    assert "2 keys" in html
    assert "found, not supported" in html


@pytest.mark.parametrize(
    ("kind", "config", "port"),
    [
        (AWG_LEGACY, "awg_legacy_wg0.conf", 55424),
        (WIREGUARD, "wireguard_wg0.conf", 51820),
    ],
)
def test_legacy_and_wireguard_servers_get_keys(
    kind: WgContainer,
    config: str,
    port: int,
    monkeypatch: pytest.MonkeyPatch,
    run: Callable[[Callable[[], Operation]], None],
) -> None:
    server_files = {
        kind.config_path: fixture(config),
        kind.clients_table_path: fixture("clientsTable.json"),
        kind.psk_path: f"{PSK}\n",
    }
    app_server = AppServer(server_files, f"{kind.name} 0.0.0.0:{port}->{port}/udp\n", kind)

    @contextmanager
    def connect(server: Server) -> Iterator[RemoteHost]:
        yield app_server.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    monkeypatch.setattr("apps.protocols.providers.connect", connect)
    server = ServerFactory()
    run(lambda: start_import(server, None))
    instance = ProtocolInstance.objects.get()

    offered = instances.get(instance.source)
    run(lambda: KeyActions(UserFactory(role=Role.ADMIN)).issue(instance.source, "iPhone", None))

    assert (instance.container, instance.port) == (kind.name, port)
    assert offered.title == f"{server.name} · {CONTAINER_TITLES[kind.name]}"
    assert offered.container == kind
    assert [target.containers for target in traffic_targets()] == [{instance.source: kind.name}]
    issued = AccessKey.objects.get(name="iPhone")
    assert issued.public_key in app_server.files[kind.config_path]
    assert len(ClientsTable.parse(app_server.files[kind.clients_table_path])) == 3
