"""XRay is installed as the Amnezia app installs it, and keeps what the volume already has."""

import json
from pathlib import Path

import pytest
from django.test import Client

from amnezia_core.clients_table import ClientsTable
from amnezia_core.xray import (
    XRAY_CLIENTS_TABLE_PATH,
    XRAY_CONFIG_PATH,
    XrayServerConfig,
    XraySettings,
)
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.services.installer import installers
from apps.protocols.services.xray_installer import XrayInstaller
from apps.protocols.tests.fake_xray_host import (
    OWN_CLIENT,
    PRIVATE_KEY,
    PUBLIC_KEY,
    SHORT_ID,
    FakeXrayHost,
)
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).resolve().parents[3] / "amnezia_core" / "tests" / "fixtures"
# server.json the app writes for the default install with the keys of FakeXrayHost.
SERVER_JSON = (FIXTURES / "xray_server.json").read_text(encoding="utf-8")


def install(server: FakeXrayHost, **params: object) -> tuple[XrayServerConfig, Operation]:
    operation = OperationFactory()
    params = {"port": 443, "logs": False, **params}
    config = XrayInstaller.from_params(server.host(), OperationContext(operation), params).run()
    return config, operation


def log_of(operation: Operation) -> str:
    return "\n".join(operation.steps.values_list("log", flat=True))


def test_install_page_offers_xray_with_app_defaults() -> None:
    installer = installers.get("amnezia-xray")

    assert installer is XrayInstaller
    assert (installer.folder, installer.default_port, installer.transport) == ("xray", 443, "tcp")
    assert installer.data_volume


def test_clean_server_gets_the_server_json_the_app_writes_without_access_log() -> None:
    server = FakeXrayHost()

    config, operation = install(server)

    app = XrayServerConfig.parse(SERVER_JSON)
    app.enable_stats()
    expected = json.loads(app.serialize())
    expected["log"]["access"] = "none"
    assert json.loads(server.volume[XRAY_CONFIG_PATH]) == expected
    assert XrayServerConfig.parse(server.volume[XRAY_CONFIG_PATH].decode()).stats_enabled
    assert config.settings() == XraySettings()
    assert server.volume[XRAY_CLIENTS_TABLE_PATH] == ClientsTable([]).serialize().encode()
    assert server.xray_up and server.tun
    assert "amnezia-dns-net" in server.container_networks
    assert server.run_arguments == [
        "docker", "run", "-d", "--privileged", "--log-driver", "none", "--restart", "always",
        "--cap-add=NET_ADMIN", "-p", "443:443/tcp", "-p", "443:443/udp",
        "-v", "amnezia-xray-data:/opt/amnezia/xray", "--name", "amnezia-xray", "amnezia-xray",
    ]  # fmt: skip
    start = server.start_script.decode()
    assert "iptables -A INPUT -p tcp --dport 443 -j ACCEPT" in start
    assert "$XRAY_SERVER_PORT" not in start
    log = log_of(operation)
    assert PUBLIC_KEY in log
    for secret in (OWN_CLIENT, PRIVATE_KEY, SHORT_ID):
        assert secret not in log


def test_custom_port_and_masking_site() -> None:
    server = FakeXrayHost()

    config, _ = install(server, port=8443, site="www.example.com", logs=True)

    settings = config.settings()
    assert (settings.port, settings.site, settings.sni) == (
        8443,
        "www.example.com",
        "www.example.com",
    )
    assert "8443:8443/tcp" in server.run_arguments
    assert "json-file" in server.run_arguments
    assert "--dport 8443" in server.start_script.decode()


def test_invalid_site_is_refused_before_anything_runs() -> None:
    server = FakeXrayHost()

    with pytest.raises(ValueError, match="site"):
        install(server, site="example.com; reboot")

    assert not server.container_state


def test_busy_tcp_port_is_refused() -> None:
    server = FakeXrayHost(port_users="nginx 812 root 6u IPv4 TCP *:443 (LISTEN)")

    with pytest.raises(SetupError) as caught:
        install(server)

    assert caught.value.code == SetupErrorCode.PORT_BUSY
    assert not server.container_state


def test_failed_key_generation_stops_the_install() -> None:
    server = FakeXrayHost(keygen_fails=True)

    with pytest.raises(SetupError) as caught:
        install(server)

    assert caught.value.code == SetupErrorCode.XRAY_KEYGEN_FAILED
    assert XRAY_CONFIG_PATH not in server.volume


def test_server_set_up_in_the_app_keeps_its_clients() -> None:
    config = XrayServerConfig.parse(SERVER_JSON)
    config.add_client("0d4f7a2b-8c3e-4f1a-9b6d-5e2c1a0f9d8e", "xtls-rprx-vision")
    kept = config.serialize().encode()
    server = FakeXrayHost(
        container_state="exited",
        container_networks={"bridge", "amnezia-dns-net"},
        start_script=b"#!/bin/bash\nxray -config /opt/amnezia/xray/server.json\n",
        volume={XRAY_CONFIG_PATH: kept, XRAY_CLIENTS_TABLE_PATH: b'[{"clientId": "x"}]'},
    )

    result, operation = install(server, port=8443)

    assert server.volume[XRAY_CONFIG_PATH] == kept
    assert len(result.client_ids) == 2
    assert server.volume[XRAY_CLIENTS_TABLE_PATH] == b'[{"clientId": "x"}]'
    assert server.start_script.startswith(b"#!/bin/bash\nxray -config")
    assert "already running" in log_of(operation)


def test_unreadable_server_json_is_not_overwritten() -> None:
    server = FakeXrayHost(container_state="running", start_script=b"tail -f /dev/null\n")
    server.volume[XRAY_CONFIG_PATH] = b'{"inbounds": []}'

    with pytest.raises(SetupError) as caught:
        install(server)

    assert caught.value.code == SetupErrorCode.XRAY_CONFIG_INVALID
    assert server.volume[XRAY_CONFIG_PATH] == b'{"inbounds": []}'


def test_empty_server_json_is_written_again() -> None:
    server = FakeXrayHost(container_state="running", start_script=b"tail -f /dev/null\n")
    server.volume[XRAY_CONFIG_PATH] = b"\n"

    config, _ = install(server)

    assert json.loads(server.volume[XRAY_CONFIG_PATH])["inbounds"][0]["port"] == 443
    assert config.client_ids == [OWN_CLIENT]


def test_xray_that_does_not_start_fails_the_install() -> None:
    server = FakeXrayHost(xray_starts=False)

    with pytest.raises(SetupError) as caught:
        install(server)

    assert caught.value.code == SetupErrorCode.XRAY_NOT_RUNNING


def test_install_page_passes_the_masking_site(admin_client: Client) -> None:
    server = ServerFactory()
    url = f"/protocols/install/{server.pk}/"

    response = admin_client.post(
        url, {"container": "amnezia-xray", "port": "", "site": " www.example.com "}
    )

    assert response.status_code == 302
    assert Operation.objects.get().params == {
        "server_id": server.pk, "container": "amnezia-xray", "port": 443, "logs": False,
        "server_host": server.host, "site": "www.example.com",
    }  # fmt: skip


def test_install_page_uses_the_app_site_and_ignores_it_for_awg(admin_client: Client) -> None:
    server = ServerFactory()
    url = f"/protocols/install/{server.pk}/"

    admin_client.post(url, {"container": "amnezia-xray", "site": ""})
    admin_client.post(url, {"container": "amnezia-awg2", "site": "not a site"})

    xray, awg = Operation.objects.order_by("pk")
    assert xray.params["site"] == "www.googletagmanager.com"
    assert "site" not in awg.params


@pytest.mark.parametrize("site", ["*.example.com", "example.com;reboot", "localhost"])
def test_install_page_refuses_a_site_the_app_refuses(admin_client: Client, site: str) -> None:
    server = ServerFactory()

    response = admin_client.post(
        f"/protocols/install/{server.pk}/", {"container": "amnezia-xray", "site": site}
    )

    assert "site" in response.context["form"].errors
    assert not Operation.objects.exists()


def test_xray_needs_its_port_free_on_udp_too(admin_client: Client) -> None:
    server = ServerFactory(containers=[{"name": "amnezia-awg2", "port": 443, "transport": "udp"}])

    response = admin_client.post(
        f"/protocols/install/{server.pk}/", {"container": "amnezia-xray", "port": "443"}
    )

    assert "port" in response.context["form"].errors


def test_installed_xray_takes_its_port_on_udp(admin_client: Client) -> None:
    server = ServerFactory()
    ProtocolInstance.objects.create(
        server=server,
        container="amnezia-xray",
        port=8443,
        public_key=PUBLIC_KEY,
        origin=InstanceOrigin.INSTALLED,
    )

    response = admin_client.post(
        f"/protocols/install/{server.pk}/", {"container": "amnezia-wireguard", "port": "8443"}
    )

    assert "port" in response.context["form"].errors
