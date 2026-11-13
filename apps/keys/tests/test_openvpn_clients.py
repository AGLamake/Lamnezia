import pytest

from amnezia_core.clients_table import ClientsTable
from amnezia_core.openvpn import (
    OPENVPN_CLIENTS_TABLE_PATH,
    OPENVPN_INDEX_PATH,
    CertificateIndex,
    ClientKey,
    OpenVpnSettings,
    certificate_path,
    certificate_serial,
    upload_path,
)
from apps.keys.errors import PeerError, PeerErrorCode
from apps.keys.services.openvpn_clients import OpenVpnClientManager
from apps.keys.tests.fake_openvpn import CRL, PENDING, PREVIOUS_CRL, TA_KEY, FakeOpenVpnServer
from apps.keys.tests.steps import RecordedSteps

CLIENT = "Kq3pX9tZ0bLmN4vR7sW2yA8cE6dF1gH5"
OTHER = "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp"


@pytest.fixture
def server() -> FakeOpenVpnServer:
    return FakeOpenVpnServer()


@pytest.fixture
def key() -> ClientKey:
    return ClientKey.generate()


def manager_for(
    server: FakeOpenVpnServer, steps: RecordedSteps | None = None
) -> OpenVpnClientManager:
    return OpenVpnClientManager(server.host(), steps or RecordedSteps())


def clients_table(server: FakeOpenVpnServer) -> ClientsTable:
    return ClientsTable.parse(server.files[OPENVPN_CLIENTS_TABLE_PATH])


def serial_of(server: FakeOpenVpnServer, client_id: str) -> int:
    return certificate_serial(server.files[certificate_path(client_id)])


def valid_clients(server: FakeOpenVpnServer) -> list[str]:
    return CertificateIndex.parse(server.files[OPENVPN_INDEX_PATH]).valid_clients()


def test_issue_signs_the_client_key_and_registers(
    server: FakeOpenVpnServer, key: ClientKey
) -> None:
    steps = RecordedSteps()

    files = manager_for(server, steps).issue(CLIENT, key, "Phone", user_id=4)

    assert server.easyrsa == [
        f"import-req {upload_path(CLIENT)} {CLIENT}",
        f"sign-req client {CLIENT}",
    ]
    assert files.settings == OpenVpnSettings()
    assert files.ca_cert == server.ca.pem
    assert files.client_cert == server.files[certificate_path(CLIENT)]
    assert files.ta_key == TA_KEY
    assert valid_clients(server) == [CLIENT]
    assert clients_table(server).user_data(CLIENT)["lamneziaUserId"] == 4
    assert server.restarts == 0
    assert "PRIVATE KEY" not in repr(files)
    assert not any("PRIVATE KEY" in text for text in server.files.values())
    assert steps.titles == [
        "Lock the server",
        "Sign the client certificate",
        "Update the client list",
        "Read the protocol configuration",
    ]


def test_issue_repeated_after_a_failure_finishes_the_job(
    server: FakeOpenVpnServer, key: ClientKey
) -> None:
    server.fail_on = "sign-req"
    with pytest.raises(PeerError) as error:
        manager_for(server).issue(CLIENT, key, "Phone")
    assert error.value.code == PeerErrorCode.CERTIFICATE_REJECTED

    server.fail_on = ""
    manager_for(server).issue(CLIENT, key, "Phone")
    manager_for(server).issue(CLIENT, key, "Phone")

    assert server.easyrsa.count(f"import-req {upload_path(CLIENT)} {CLIENT}") == 1
    assert valid_clients(server) == [CLIENT]


def test_disable_puts_the_certificate_in_the_crl_and_enable_takes_it_out(
    server: FakeOpenVpnServer, key: ClientKey
) -> None:
    manager = manager_for(server)
    manager.issue(CLIENT, key, "Phone")
    serial = serial_of(server, CLIENT)

    manager.disable(CLIENT)
    assert server.refused() == {serial}
    assert server.restarts == 1
    assert clients_table(server).name(CLIENT) == "Phone"
    assert PENDING not in server.files

    manager.disable(CLIENT)
    assert server.restarts == 1  # nothing changed, so openvpn keeps running

    manager.enable(CLIENT, "Phone")
    assert server.refused() == set()
    assert server.restarts == 2
    assert valid_clients(server) == [CLIENT]


def test_revoke_runs_easyrsa_even_for_a_disabled_key(
    server: FakeOpenVpnServer, key: ClientKey
) -> None:
    manager = manager_for(server)
    manager.issue(CLIENT, key, "Phone")
    serial = serial_of(server, CLIENT)
    manager.disable(CLIENT)

    manager.revoke(CLIENT)
    manager.revoke(CLIENT)

    assert server.easyrsa.count(f"revoke {CLIENT}") == 1
    assert server.refused() == {serial}
    assert CLIENT not in clients_table(server)
    assert certificate_path(CLIENT) not in server.files


def test_an_interrupted_change_is_applied_when_repeated(
    server: FakeOpenVpnServer, key: ClientKey
) -> None:
    manager = manager_for(server)
    manager.issue(CLIENT, key, "Phone")
    server.fail_on = "gen-crl"
    with pytest.raises(PeerError):
        manager.disable(CLIENT)
    assert server.refused() == set()

    server.fail_on = ""
    manager.disable(CLIENT)

    assert server.refused() == {serial_of(server, CLIENT)}
    assert PENDING not in server.files


def test_openvpn_that_does_not_come_back_is_reported(
    server: FakeOpenVpnServer, key: ClientKey
) -> None:
    manager = manager_for(server)
    manager.issue(CLIENT, key, "Phone")
    server.starts = False

    with pytest.raises(PeerError) as error:
        manager.disable(CLIENT)

    assert error.value.code == PeerErrorCode.OPENVPN_DOWN
    assert PENDING in server.files  # the next run restarts it again


def test_openvpn_comes_back_with_the_previous_crl_when_the_new_one_fails(
    server: FakeOpenVpnServer, key: ClientKey
) -> None:
    manager = manager_for(server)
    manager.issue(CLIENT, key, "Phone")
    manager.issue(OTHER, ClientKey.generate(), "Laptop")
    manager.disable(OTHER)
    previous = server.files[CRL]
    server.breaks_crl = True

    with pytest.raises(PeerError) as error:
        manager.disable(CLIENT)

    assert error.value.code == PeerErrorCode.OPENVPN_DOWN
    assert server.running and server.files[CRL] == previous
    assert server.refused() == {serial_of(server, OTHER)}
    assert PENDING in server.files

    server.breaks_crl = False
    manager.disable(CLIENT)

    assert server.refused() == {serial_of(server, OTHER), serial_of(server, CLIENT)}
    assert PENDING not in server.files and PREVIOUS_CRL not in server.files


def test_enable_of_a_certificate_revoked_elsewhere_fails(
    server: FakeOpenVpnServer, key: ClientKey
) -> None:
    manager = manager_for(server)
    manager.issue(CLIENT, key, "Phone")
    manager.revoke(CLIENT)

    with pytest.raises(PeerError) as error:
        manager.enable(CLIENT, "Phone")

    assert error.value.code == PeerErrorCode.CERTIFICATE_GONE


def test_rename_and_assign_touch_only_the_client_list(
    server: FakeOpenVpnServer, key: ClientKey
) -> None:
    manager = manager_for(server)
    manager.issue(CLIENT, key, "Phone")

    manager.rename(CLIENT, "Laptop")
    manager.assign(CLIENT, "Laptop", user_id=7)

    assert clients_table(server).name(CLIENT) == "Laptop"
    assert clients_table(server).user_data(CLIENT)["lamneziaUserId"] == 7
    assert server.restarts == 0


@pytest.mark.parametrize("client_id", ["x;reboot", "AmneziaReq", "../ca"])
def test_unsafe_ids_never_reach_the_server(server: FakeOpenVpnServer, client_id: str) -> None:
    manager = manager_for(server)

    for call in (manager.disable, manager.revoke):
        with pytest.raises(PeerError):
            call(client_id)

    assert server.easyrsa == []
