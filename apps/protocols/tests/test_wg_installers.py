"""AmneziaWG legacy and WireGuard are installed as the Amnezia app installs them."""

from ipaddress import IPv4Interface

import pytest

from amnezia_core.awg import AwgServerConfig
from amnezia_core.awg_export import AwgClientProfile, AwgServerSettings
from amnezia_core.awg_install import AwgInstallSettings
from amnezia_core.clients_table import ClientsTable
from amnezia_core.containers import AWG_LEGACY, WIREGUARD, WgContainer
from amnezia_core.keys import KeyPair
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.services.awg_installer import (
    AwgInstaller,
    AwgLegacyInstaller,
    WgInstaller,
    WireGuardInstaller,
)
from apps.protocols.services.installer import installers
from apps.protocols.tests.fake_awg import START_SCRIPT, FakeAwgServer

pytestmark = pytest.mark.django_db

# Upstream checks the kernel only for AmneziaWG 2.0.
OLD_KERNEL = "4.9.0-19-amd64"


def install(
    installer: type[WgInstaller], server: FakeAwgServer
) -> tuple[AwgServerConfig, Operation]:
    operation = OperationFactory()
    params = {"port": installer.default_port, "logs": False}
    config = installer.from_params(server.host(), OperationContext(operation), params).run()
    return config, operation


def profile(server: FakeAwgServer, config: AwgServerConfig, kind: WgContainer) -> str:
    return AwgClientProfile(
        host="203.0.113.7",
        server=AwgServerSettings.from_config(config),
        server_public_key=server.server_keys.public_key,
        keys=KeyPair.generate(),
        preshared_key="",
        client_ip=IPv4Interface("10.8.1.2/32").ip,
        container=kind,
    ).native_config()


def test_install_page_offers_the_three_protocols_in_app_order() -> None:
    offered = [installer for installer in installers if issubclass(installer, WgInstaller)]

    assert offered == [AwgInstaller, AwgLegacyInstaller, WireGuardInstaller]
    assert [(i.container, i.folder, i.default_port) for i in offered] == [
        ("amnezia-awg2", "awg", 55424),
        ("amnezia-awg", "awg_legacy", 55424),
        ("amnezia-wireguard", "wireguard", 51820),
    ]


def test_legacy_awg_gets_wg0_conf_with_app_defaults() -> None:
    server = FakeAwgServer(kind=AWG_LEGACY, kernel=OLD_KERNEL)

    config, _ = install(AwgLegacyInstaller, server)

    assert server.published_port == 55424
    assert (
        "amneziavpn/amnezia-wg" in server.host_files["/opt/amnezia/amnezia-awg/Dockerfile"].decode()
    )
    assert config.listen_port == 55424
    assert config.address == IPv4Interface("10.8.1.0/24")
    assert 4 <= int(config.awg_params["Jc"]) <= 6
    assert (config.awg_params["Jmin"], config.awg_params["S1"], config.awg_params["H4"]) == (
        "10",
        "12",
        "4",
    )
    # The legacy script keeps I1 only as a comment, where the app reads it back.
    assert config.awg_params["I1"].startswith("<r 2>")
    assert (
        len(ClientsTable.parse(server.container_files[AWG_LEGACY.clients_table_path].decode())) == 0
    )
    start = server.container_files[START_SCRIPT].decode()
    assert "wg-quick up /opt/amnezia/awg/wg0.conf" in start
    assert "-s 10.8.1.0/24 -o eth0 -j MASQUERADE" in start
    assert server.awg_up
    assert "Jc = " in profile(server, config, AWG_LEGACY)


def test_wireguard_gets_plain_wg0_conf_on_51820() -> None:
    server = FakeAwgServer(kind=WIREGUARD, kernel=OLD_KERNEL)

    config, operation = install(WireGuardInstaller, server)

    assert server.published_port == 51820
    assert (
        server.container_files[WIREGUARD.config_path]
        == (
            f"[Interface]\nPrivateKey = {server.server_keys.private_key}\n"
            "Address = 10.8.1.0/24\nListenPort = 51820\n"
        ).encode()
    )
    assert config.awg_params == {}
    start = server.container_files[START_SCRIPT].decode()
    assert "wg-quick up /opt/amnezia/wireguard/wg0.conf" in start
    assert "-s 10.8.1.0/24 -o eth0 -j MASQUERADE" in start
    assert server.awg_up
    native = profile(server, config, WIREGUARD)
    assert "Endpoint = 203.0.113.7:51820" in native
    # The keys are random base64, so "Jc" alone may turn up in them.
    assert "Jc = " not in native
    log = "\n".join(operation.steps.values_list("log", flat=True))
    assert "WireGuard listens on UDP 51820" in log


def test_wireguard_settings_have_no_obfuscation() -> None:
    settings = AwgInstallSettings.for_container(WIREGUARD, 51820)

    variables = settings.script_variables()

    assert settings.params == {}
    assert variables["$CONTAINER_NAME"] == "amnezia-wireguard"
    assert (variables["$WIREGUARD_SUBNET_IP"], variables["$WIREGUARD_SERVER_PORT"]) == (
        "10.8.1.0",
        "51820",
    )
    assert variables["$JUNK_PACKET_COUNT"] == ""


def test_second_run_keeps_the_wireguard_config() -> None:
    server = FakeAwgServer(kind=WIREGUARD)
    install(WireGuardInstaller, server)
    files = dict(server.container_files)
    server.docker_calls.clear()

    _, operation = install(WireGuardInstaller, server)

    assert server.container_files == files
    assert "build" not in server.docker_calls
    assert "wg0.conf already exists and is kept." in "\n".join(
        operation.steps.values_list("log", flat=True)
    )
