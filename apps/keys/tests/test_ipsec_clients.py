import base64

import pytest

from amnezia_core.clients_table import ClientsTable
from amnezia_core.ipsec import IPSEC_CLIENTS_TABLE_PATH, export_path
from apps.keys.errors import PeerError, PeerErrorCode
from apps.keys.services.ipsec_clients import IpsecClientManager
from apps.keys.tests.fake_ipsec import P12, SERVER_IP, FakeIpsecServer
from apps.keys.tests.steps import RecordedSteps

pytestmark = pytest.mark.django_db

CLIENT = "Kq3pX9tZ0bLmN4vR7sW2yA8cE6dF1gH5"
OTHER = "Zz9yX8wV7uT6sR5qP4oN3mL2kJ1iH0gF"


@pytest.fixture
def server() -> FakeIpsecServer:
    return FakeIpsecServer()


def manager_for(server: FakeIpsecServer, steps: RecordedSteps | None = None) -> IpsecClientManager:
    return IpsecClientManager(server.host(), steps or RecordedSteps())


def clients_table(server: FakeIpsecServer) -> ClientsTable:
    return ClientsTable.parse(server.files[IPSEC_CLIENTS_TABLE_PATH].decode())


def test_issue_signs_exports_and_registers_the_client(server: FakeIpsecServer) -> None:
    issued = manager_for(server).issue(CLIENT, "Phone", user_id=7)

    assert server.signed == [CLIENT]
    assert base64.b64decode(issued.p12) == P12 + CLIENT.encode()
    assert (issued.ca_cert, issued.server_ip) == (server.ca_base64, SERVER_IP)
    assert export_path(CLIENT) not in server.files, "the exported key stays in the container"
    assert CLIENT in clients_table(server)
    assert "p12" not in repr(issued) and issued.p12 not in repr(issued)


def test_issue_again_exports_the_same_certificate(server: FakeIpsecServer) -> None:
    manager = manager_for(server)
    manager.issue(CLIENT, "Phone")

    manager.issue(CLIENT, "Phone")

    assert server.signed == [CLIENT]


def test_disable_puts_the_certificate_into_the_crl_and_ends_only_its_sessions(
    server: FakeIpsecServer,
) -> None:
    manager = manager_for(server)
    manager.issue(CLIENT, "Phone")
    manager.issue(OTHER, "Laptop")
    server.sessions = {CLIENT: [5, 3], OTHER: [4]}

    manager.disable(CLIENT)

    serial = server.certificates[CLIENT]
    assert server.crl == {serial}
    assert server.loaded_crl == {serial}
    assert server.sessions == {OTHER: [4]}
    assert CLIENT in clients_table(server)


def test_disable_twice_adds_the_serial_once(server: FakeIpsecServer) -> None:
    manager = manager_for(server)
    manager.issue(CLIENT, "Phone")
    manager.disable(CLIENT)

    manager.disable(CLIENT)

    assert server.crl == {server.certificates[CLIENT]}


def test_disable_of_a_missing_certificate_is_no_failure(server: FakeIpsecServer) -> None:
    steps = RecordedSteps()

    manager_for(server, steps).disable(CLIENT)

    assert server.crl is None
    assert "The certificate is already gone." in steps.lines


def test_enable_takes_the_certificate_out_of_the_crl(server: FakeIpsecServer) -> None:
    manager = manager_for(server)
    manager.issue(CLIENT, "Phone")
    manager.issue(OTHER, "Laptop")
    manager.disable(CLIENT)
    manager.disable(OTHER)

    manager.enable(CLIENT, "Phone")

    assert server.crl == server.loaded_crl == {server.certificates[OTHER]}


def test_enable_without_the_certificate_is_refused(server: FakeIpsecServer) -> None:
    with pytest.raises(PeerError) as caught:
        manager_for(server).enable(CLIENT, "Phone")

    assert caught.value.code == PeerErrorCode.CERTIFICATE_GONE


def test_revoke_blocks_and_deletes_the_certificate(server: FakeIpsecServer) -> None:
    manager = manager_for(server)
    manager.issue(CLIENT, "Phone")
    serial = server.certificates[CLIENT]
    server.sessions = {CLIENT: [9]}

    manager.revoke(CLIENT)

    assert CLIENT not in server.certificates
    assert server.crl == server.loaded_crl == {serial}
    assert not server.sessions
    assert CLIENT not in clients_table(server)


def test_revoke_of_a_deleted_certificate_cleans_the_client_list(server: FakeIpsecServer) -> None:
    manager = manager_for(server)
    manager.issue(CLIENT, "Phone")
    server.certificates.clear()

    manager.revoke(CLIENT)

    assert CLIENT not in clients_table(server)


def test_rename_changes_only_the_client_list(server: FakeIpsecServer) -> None:
    manager = manager_for(server)
    manager.issue(CLIENT, "Phone")

    manager.rename(CLIENT, "Tablet")

    assert clients_table(server).name(CLIENT) == "Tablet"
    assert server.signed == [CLIENT]


@pytest.mark.parametrize("client_id", ["", "a b", "x;rm -rf /", "a" * 65])
def test_bad_client_id_is_refused_before_anything_runs(
    server: FakeIpsecServer, client_id: str
) -> None:
    with pytest.raises(PeerError) as caught:
        manager_for(server).issue(client_id, "Phone")

    assert caught.value.code == PeerErrorCode.SERVER_FILES_INVALID
    assert not server.signed


def test_refused_signing_is_reported(server: FakeIpsecServer) -> None:
    server.fail_on = "certutil -z"

    with pytest.raises(PeerError) as caught:
        manager_for(server).issue(CLIENT, "Phone")

    assert caught.value.code == PeerErrorCode.IPSEC_REJECTED
    assert IPSEC_CLIENTS_TABLE_PATH not in server.files
