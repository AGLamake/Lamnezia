"""A server with IPsec set up in the Amnezia app is read into the panel with its clients."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from typing import Any

import pytest

from amnezia_core.clients_table import ClientsTable
from amnezia_core.ipsec import IPSEC_CLIENTS_TABLE_PATH, ca_fingerprint
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.tests.fake_ipsec import SERVER_IP, FakeIpsecServer
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.importer import start_import
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import CommandResult, RemoteHost
from apps.ssh.tests.fakes import result

pytestmark = pytest.mark.django_db

ANNA = "AnnaPhone0123456"
OLD = "OldLaptop0123456"
UNNAMED = "Unnamed012345678"
DOCKER_PS = "amnezia-ipsec 0.0.0.0:500->500/udp, 0.0.0.0:4500->4500/udp\n"
OnCommit = Callable[..., AbstractContextManager[Any]]


class IpsecAppServer(FakeIpsecServer):
    """IPsec set up in the Amnezia app: three clients, one of them in the CRL."""

    def __init__(self) -> None:
        super().__init__()
        for number, client_id in enumerate((ANNA, OLD, UNNAMED), start=1):
            self.certificates[client_id] = 0x2000 + number
        self.crl = {self.certificates[OLD]}
        clients = ClientsTable([])
        clients.add(ANNA, "Телефон Анны", datetime(2026, 1, 5, 12, 0))
        self.files[IPSEC_CLIENTS_TABLE_PATH] = clients.serialize().encode()
        self.writes: list[str] = []

    def handle(self, script: str, stdin: bytes) -> CommandResult:
        if "docker ps" in script:
            return result(stdout=DOCKER_PS.encode())
        if "target=" in script or "crlutil -M" in script or "certutil -S" in script:
            self.writes.append(script)
        return super().handle(script, stdin)


@pytest.fixture
def app_server(monkeypatch: pytest.MonkeyPatch) -> IpsecAppServer:
    server = IpsecAppServer()

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


def test_import_takes_the_clients_in_force_and_writes_nothing(
    app_server: IpsecAppServer, django_capture_on_commit_callbacks: OnCommit
) -> None:
    server = ServerFactory(host="vpn.example.com")

    operation = run_import(server, django_capture_on_commit_callbacks)

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    instance = ProtocolInstance.objects.get(server=server, container="amnezia-ipsec")
    assert (instance.port, instance.subnet) == (500, "192.168.43.0/24")
    assert instance.params == {"server_ip": SERVER_IP}
    assert instance.public_key == ca_fingerprint(app_server.ca_base64)
    keys = {key.public_key: key for key in AccessKey.objects.filter(source=instance.source)}
    assert set(keys) == {ANNA, UNNAMED}
    assert keys[ANNA].name == "Телефон Анны"
    assert keys[ANNA].status == KeyStatus.ACTIVE
    assert not keys[ANNA].can_export
    assert app_server.writes == []


def test_import_again_adds_only_new_clients(
    app_server: IpsecAppServer, django_capture_on_commit_callbacks: OnCommit
) -> None:
    server = ServerFactory()
    run_import(server, django_capture_on_commit_callbacks)
    app_server.certificates["NewTablet0123456"] = 0x3000

    run_import(server, django_capture_on_commit_callbacks)

    assert AccessKey.objects.count() == 3
