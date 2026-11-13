"""A server with OpenVPN set up in the Amnezia app is read into the panel with its clients."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from amnezia_core.clients_table import ClientsTable
from amnezia_core.openvpn import (
    OPENVPN,
    OPENVPN_CLIENTS_TABLE_PATH,
    OPENVPN_CONFIG_PATH,
    OPENVPN_INDEX_PATH,
    ca_fingerprint,
)
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import instances
from apps.keys.tests.fake_openvpn import FakeOpenVpnServer
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.services.importer import start_import
from apps.protocols.services.inventory import server_containers
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import CommandResult, RemoteHost
from apps.ssh.tests.fakes import result

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).resolve().parents[3] / "amnezia_core" / "tests" / "fixtures"
# The certificates in force in openvpn_index.txt, besides the server's own.
ANNA = "Kq3pX9tZ0bLmN4vR7sW2yA8cE6dF1gH5"
UNNAMED = "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp"
DOCKER_PS = "amnezia-openvpn 0.0.0.0:1194->1194/udp\n"
OnCommit = Callable[..., AbstractContextManager[Any]]


class OpenVpnAppServer(FakeOpenVpnServer):
    """OpenVPN set up in the Amnezia app, answering `docker ps` too."""

    def __init__(self) -> None:
        super().__init__()
        clients = ClientsTable([])
        clients.add(ANNA, "Телефон Анны", datetime(2026, 1, 5, 12, 0))
        self.files[OPENVPN_INDEX_PATH] = (FIXTURES / "openvpn_index.txt").read_text(
            encoding="utf-8"
        )
        self.files[OPENVPN_CLIENTS_TABLE_PATH] = clients.serialize()
        self.commands: list[str] = []

    def handle(self, script: str, stdin: bytes) -> CommandResult:
        self.commands.append(script)
        if "docker ps" in script:
            return result(stdout=DOCKER_PS.encode())
        return super().handle(script, stdin)


@pytest.fixture
def app_server(monkeypatch: pytest.MonkeyPatch) -> OpenVpnAppServer:
    server = OpenVpnAppServer()

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
def imported(app_server: OpenVpnAppServer, django_capture_on_commit_callbacks: OnCommit) -> Server:
    server = ServerFactory(host="203.0.113.10")
    operation = run_import(server, django_capture_on_commit_callbacks)
    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    return server


def test_import_records_openvpn_with_its_settings(
    app_server: OpenVpnAppServer, imported: Server
) -> None:
    instance = ProtocolInstance.objects.get()

    assert (instance.server, instance.container, instance.port) == (
        imported,
        "amnezia-openvpn",
        1194,
    )
    assert instance.origin == InstanceOrigin.IMPORTED
    assert instance.params == {
        "transport": "udp",
        "cipher": "AES-256-GCM",
        "hash": "SHA512",
        "ncp_disable": "false",
        "tls_auth": "true",
        "additional_server_config": "",
    }
    assert instance.public_key == ca_fingerprint(app_server.ca.pem)
    assert instance.private_key == ""
    assert instance.title == "OpenVPN"


def test_certificates_in_force_become_ownerless_keys(imported: Server) -> None:
    keys = {key.public_key: key for key in AccessKey.objects.all()}

    assert set(keys) == {ANNA, UNNAMED}  # neither the server's own nor the revoked one
    anna = keys[ANNA]
    assert (anna.name, anna.status, anna.user) == ("Телефон Анны", KeyStatus.ACTIVE, None)
    assert not anna.can_export  # the private key stays on the device
    assert keys[UNNAMED].name == "Imported client 2"


def test_import_changes_nothing_on_the_server(
    app_server: OpenVpnAppServer, imported: Server
) -> None:
    assert not [c for c in app_server.commands if "target=" in c or "easyrsa" in c]
    assert app_server.restarts == 0


def test_import_again_adds_only_new_clients(
    app_server: OpenVpnAppServer, imported: Server, django_capture_on_commit_callbacks: OnCommit
) -> None:
    AccessKey.objects.filter(public_key=ANNA).update(name="Renamed in the panel")
    app_server.files[OPENVPN_INDEX_PATH] += (
        "V\t280210120000Z\t\t0D\tunknown\t/CN=Nn1Mm2Ll3Kk4Jj5Hh6Gg7Ff8Ee9Dd0Cc\n"
    )

    run_import(imported, django_capture_on_commit_callbacks)

    assert ProtocolInstance.objects.count() == 1
    assert AccessKey.objects.count() == 3
    assert AccessKey.objects.get(public_key=ANNA).name == "Renamed in the panel"


def test_import_again_reads_the_extra_server_config_and_keeps_the_panel_options(
    app_server: OpenVpnAppServer, imported: Server, django_capture_on_commit_callbacks: OnCommit
) -> None:
    instance = ProtocolInstance.objects.get()
    instance.params = {
        **instance.params, "additional_client_config": "mssfix 1300", "block_outside_dns": "true",
    }  # fmt: skip
    instance.save(update_fields=["params"])
    app_server.files[OPENVPN_CONFIG_PATH] += "max-clients 50\n"

    run_import(imported, django_capture_on_commit_callbacks)

    params = ProtocolInstance.objects.get().params
    assert params["additional_server_config"] == "max-clients 50"
    assert params["additional_client_config"] == "mssfix 1300"
    assert params["block_outside_dns"] == "true"


def test_certificate_with_an_unsafe_name_is_skipped(
    app_server: OpenVpnAppServer, django_capture_on_commit_callbacks: OnCommit
) -> None:
    app_server.files[OPENVPN_INDEX_PATH] += "V\t280210120000Z\t\t0E\tunknown\t/CN=x;reboot\n"

    operation = run_import(ServerFactory(), django_capture_on_commit_callbacks)

    assert operation.status == OperationStatus.SUCCEEDED
    assert set(AccessKey.objects.values_list("public_key", flat=True)) == {ANNA, UNNAMED}
    log = "\n".join(operation.steps.values_list("log", flat=True))
    assert "cannot manage are skipped: 1" in log


def test_broken_server_conf_fails_with_a_reason(
    app_server: OpenVpnAppServer, django_capture_on_commit_callbacks: OnCommit
) -> None:
    app_server.files[OPENVPN_CONFIG_PATH] = "port x\n"

    operation = run_import(ServerFactory(), django_capture_on_commit_callbacks)

    assert operation.status == OperationStatus.FAILED
    assert not ProtocolInstance.objects.exists()


def test_card_counts_the_openvpn_keys(imported: Server) -> None:
    imported.refresh_from_db()

    rows = server_containers(imported)

    assert [(row.name, row.port, row.supported, row.keys) for row in rows] == [
        ("amnezia-openvpn", 1194, True, 2)
    ]


def test_keys_can_be_issued_on_the_imported_openvpn(imported: Server) -> None:
    target = instances.get(ProtocolInstance.objects.get().source)

    assert target.container is OPENVPN
    assert target.host == "203.0.113.10"
