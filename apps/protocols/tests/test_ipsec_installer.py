"""IPsec is installed as the Amnezia app installs it, and keeps a CA already in the container."""

import socket

import pytest
from django.test import Client

from amnezia_core.clients_table import ClientsTable
from amnezia_core.ipsec import IKEV2_CONFIG_PATH, IPSEC_CLIENTS_TABLE_PATH, server_identity
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.installer import installers
from apps.protocols.services.ipsec_container import configure_script
from apps.protocols.services.ipsec_installer import IpsecInstaller
from apps.protocols.tests.fake_ipsec_host import FakeIpsecHost
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

SERVER_IP = "203.0.113.7"
IKEV2_CONF = f"conn ikev2-cp\n  leftcert={SERVER_IP}\n  leftid={SERVER_IP}\n"


def install(server: FakeIpsecHost, **params: object) -> Operation:
    operation = OperationFactory()
    params = {"port": 500, "logs": False, "server_host": SERVER_IP, **params}
    installer = IpsecInstaller.from_params(server.host(), OperationContext(operation), params)
    installer.run()
    return operation


def configured(**fields: object) -> FakeIpsecHost:
    files = {IKEV2_CONFIG_PATH: IKEV2_CONF.encode()}
    return FakeIpsecHost(container_state="running", has_ca=True, files=files, **fields)  # type: ignore[arg-type]


def test_install_page_offers_ipsec_on_the_fixed_ports() -> None:
    installer = installers.get("amnezia-ipsec")

    assert installer is IpsecInstaller
    assert installer.fixed_port
    assert installer.published_ports(40000) == {(500, "udp"), (4500, "udp")}


def test_clean_server_gets_the_ca_and_ikev2_conf_the_app_writes() -> None:
    server = FakeIpsecHost()

    install(server)

    conf = server.files[IKEV2_CONFIG_PATH].decode()
    assert server_identity(conf) == SERVER_IP
    assert "modecfgdns=8.8.8.8,8.8.4.4" in conf
    assert server.files[IPSEC_CLIENTS_TABLE_PATH] == ClientsTable([]).serialize().encode()
    assert server.pluto_up
    assert (server.pki_runs, server.resets) == (1, 1)
    assert "amnezia-dns-net" in server.container_networks
    assert server.run_arguments == [
        "docker", "run", "-d", "--privileged", "--log-driver", "none", "--restart", "always",
        "-p", "500:500/udp", "-p", "4500:4500/udp", "--name", "amnezia-ipsec", "amnezia-ipsec",
    ]  # fmt: skip


def test_both_ports_are_checked_before_the_build() -> None:
    server = FakeIpsecHost(port_users={4500: "charon 123 root UDP *:4500"})

    with pytest.raises(SetupError) as caught:
        install(server)

    assert caught.value.code == SetupErrorCode.PORT_BUSY
    assert not server.container_state


def test_server_given_by_name_is_resolved_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *_args: [(socket.AF_INET, 0, 0, "", (SERVER_IP, 0))]
    )
    server = FakeIpsecHost()

    install(server, server_host="vpn.example.com")

    assert server_identity(server.files[IKEV2_CONFIG_PATH].decode()) == SERVER_IP


def test_failed_ca_setup_stops_the_install_and_a_retry_starts_over() -> None:
    server = FakeIpsecHost(pki_fails=True)

    with pytest.raises(SetupError) as caught:
        install(server)

    assert caught.value.code == SetupErrorCode.IPSEC_SETUP_FAILED

    server.pki_fails = False
    install(server)

    assert (server.pki_runs, server.resets) == (2, 2)
    assert server.pluto_up


def test_ca_of_the_container_is_kept_and_pluto_started_again() -> None:
    clients = ClientsTable([]).serialize().encode()
    server = configured()
    server.files[IPSEC_CLIENTS_TABLE_PATH] = clients

    operation = install(server)

    assert (server.pki_runs, server.resets, server.service_runs) == (0, 0, 1)
    assert server.files[IKEV2_CONFIG_PATH].decode() == IKEV2_CONF
    assert server.files[IPSEC_CLIENTS_TABLE_PATH] == clients
    assert server.pluto_up
    assert "is kept" in "\n".join(operation.steps.values_list("log", flat=True))


def test_running_pluto_is_left_alone() -> None:
    server = configured(pluto_up=True)

    install(server)

    assert server.service_runs == 0


def test_broken_ikev2_conf_in_the_container_is_reported() -> None:
    server = configured()
    server.files[IKEV2_CONFIG_PATH] = b"conn ikev2-cp\n"

    with pytest.raises(SetupError) as caught:
        install(server)

    assert caught.value.code == SetupErrorCode.IPSEC_CONFIG_INVALID


def test_pluto_that_does_not_start_fails_the_install() -> None:
    server = FakeIpsecHost(pluto_starts=False)

    with pytest.raises(SetupError) as caught:
        install(server)

    assert caught.value.code == SetupErrorCode.IPSEC_NOT_RUNNING


def test_service_part_of_the_script_makes_no_certificates() -> None:
    services = configure_script(SERVER_IP, with_pki=False)
    full = configure_script(SERVER_IP, with_pki=True)

    assert "certutil" not in services and "certutil -z" in full
    assert services.rstrip().endswith("ipsec auto --add ikev2-cp")
    assert f"leftid={SERVER_IP}" in services


def test_install_page_ignores_a_port_for_ipsec(admin_client: Client) -> None:
    server = ServerFactory()
    url = f"/protocols/install/{server.pk}/"

    page = admin_client.get(url)
    admin_client.post(url, {"container": "amnezia-ipsec", "port": "40000"})

    assert "500/udp, 4500/udp" in page.content.decode()
    assert Operation.objects.get().params["port"] == 500


def test_install_page_refuses_ipsec_when_4500_is_taken(admin_client: Client) -> None:
    server = ServerFactory(containers=[{"name": "amnezia-awg2", "port": 4500, "transport": "udp"}])

    response = admin_client.post(f"/protocols/install/{server.pk}/", {"container": "amnezia-ipsec"})

    assert "port" in response.context["form"].errors
