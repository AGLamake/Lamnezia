"""OpenVPN is installed as the Amnezia app installs it, and keeps a CA already in the container."""

import socket
from pathlib import Path

import pytest

from amnezia_core.clients_table import ClientsTable
from amnezia_core.openvpn import OPENVPN_CLIENTS_TABLE_PATH, OPENVPN_CONFIG_PATH, OpenVpnSettings
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.installer import installers
from apps.protocols.services.openvpn_installer import OpenVpnInstaller
from apps.protocols.tests.fake_openvpn_host import CA_CERT, CRL, PKI_FILES, FakeOpenVpnHost

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).resolve().parents[3] / "amnezia_core" / "tests" / "fixtures"
# server.conf the app writes for the default install.
SERVER_CONF = (FIXTURES / "openvpn_server.conf").read_text(encoding="utf-8")
SERVER_IP = "203.0.113.7"


def install(server: FakeOpenVpnHost, **params: object) -> tuple[OpenVpnSettings, Operation]:
    operation = OperationFactory()
    params = {"port": 1194, "logs": False, "server_host": SERVER_IP, **params}
    installer = OpenVpnInstaller.from_params(server.host(), OperationContext(operation), params)
    return installer.run(), operation


def test_install_page_offers_openvpn_with_app_defaults() -> None:
    installer = installers.get("amnezia-openvpn")

    assert installer is OpenVpnInstaller
    assert (installer.folder, installer.default_port, installer.transport) == (
        "openvpn",
        1194,
        "udp",
    )
    assert not installer.data_volume


def test_clean_server_gets_the_ca_and_the_server_conf_the_app_writes() -> None:
    server = FakeOpenVpnHost()

    settings, _ = install(server)

    assert settings == OpenVpnSettings()
    assert server.files[OPENVPN_CONFIG_PATH].decode().strip() == SERVER_CONF.strip()
    assert CA_CERT in server.files and CRL in server.files
    assert server.files[OPENVPN_CLIENTS_TABLE_PATH] == ClientsTable([]).serialize().encode()
    assert server.openvpn_up
    assert "amnezia-dns-net" in server.container_networks
    assert server.run_arguments == [
        "docker", "run", "-d", "--privileged", "--log-driver", "none", "--restart", "always",
        "--cap-add=NET_ADMIN", "-p", "1194:1194/udp", "--name", "amnezia-openvpn",
        "amnezia-openvpn",
    ]  # fmt: skip
    start = server.files["/opt/amnezia/start.sh"].decode()
    assert f"ifconfig eth0:0 {SERVER_IP} netmask" in start
    assert "-s 10.8.0.0/24 -o eth0" in start
    assert f"ifconfig eth0:0 {SERVER_IP} netmask 255.255.255.255 up" in server.container_scripts


def test_custom_port_reaches_server_conf_and_docker() -> None:
    server = FakeOpenVpnHost()

    settings, _ = install(server, port=11940, logs=True)

    assert settings.port == 11940
    assert "port 11940\n" in server.files[OPENVPN_CONFIG_PATH].decode()
    assert "11940:11940/udp" in server.run_arguments
    assert "json-file" in server.run_arguments


def test_server_given_by_name_is_resolved_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *_args: [(socket.AF_INET, 0, 0, "", (SERVER_IP, 0))]
    )
    server = FakeOpenVpnHost()

    _, operation = install(server, server_host="vpn.example.com")

    assert f"ifconfig eth0:0 {SERVER_IP} netmask" in server.files["/opt/amnezia/start.sh"].decode()
    assert SERVER_IP in "\n".join(operation.steps.values_list("log", flat=True))


def test_unknown_server_name_is_refused_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unknown(*_args: object) -> object:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", unknown)
    server = FakeOpenVpnHost()

    with pytest.raises(SetupError) as caught:
        install(server, server_host="nowhere.invalid")

    assert caught.value.code == SetupErrorCode.HOST_NOT_RESOLVED
    assert not server.container_state


def test_failed_ca_setup_stops_the_install_and_a_retry_starts_over() -> None:
    server = FakeOpenVpnHost(pki_fails=True)

    with pytest.raises(SetupError) as caught:
        install(server)

    assert caught.value.code == SetupErrorCode.OPENVPN_PKI_FAILED
    assert OPENVPN_CONFIG_PATH not in server.files

    server.pki_fails = False
    install(server)

    assert server.pki_runs == 2
    assert server.openvpn_up


def test_ca_and_config_of_the_container_are_kept() -> None:
    custom = SERVER_CONF.replace("server 10.8.0.0 ", "server 10.9.0.0 ")
    clients = ClientsTable([]).serialize().encode()
    server = FakeOpenVpnHost(
        container_state="running",
        files={
            **PKI_FILES,
            CRL: b"old crl\n",
            OPENVPN_CONFIG_PATH: custom.encode(),
            OPENVPN_CLIENTS_TABLE_PATH: clients,
        },
    )

    settings, operation = install(server)

    assert server.pki_runs == 0
    assert server.files[CRL] == b"old crl\n"
    assert server.files[OPENVPN_CONFIG_PATH].decode() == custom
    assert str(settings.subnet) == "10.9.0.0/24"
    assert "-s 10.9.0.0/24" in server.files["/opt/amnezia/start.sh"].decode()
    assert "already exists and is kept" in "\n".join(operation.steps.values_list("log", flat=True))


def test_broken_server_conf_in_the_container_is_reported() -> None:
    server = FakeOpenVpnHost(
        container_state="running",
        files={**PKI_FILES, CRL: b"crl\n", OPENVPN_CONFIG_PATH: b"port x\n"},
    )

    with pytest.raises(SetupError) as caught:
        install(server)

    assert caught.value.code == SetupErrorCode.OPENVPN_CONFIG_INVALID


def test_openvpn_that_does_not_start_fails_the_install() -> None:
    server = FakeOpenVpnHost(openvpn_starts=False)

    with pytest.raises(SetupError) as caught:
        install(server)

    assert caught.value.code == SetupErrorCode.OPENVPN_NOT_RUNNING
