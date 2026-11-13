"""Profiles of the legacy AmneziaWG and WireGuard containers, next to AmneziaWG 2.0."""

import json
from collections.abc import Callable
from ipaddress import IPv4Address

from amnezia_core.awg import AwgServerConfig
from amnezia_core.awg_export import AwgClientProfile, AwgServerSettings
from amnezia_core.containers import AWG_LEGACY, WIREGUARD, WgContainer
from amnezia_core.keys import KeyPair

CLIENT_KEYS = KeyPair.from_private_key("dwdtCnMYpX08FsFyUbJmRd9ML4frwJkqsXf7pR25LCo=")
SERVER_PUBLIC = "WAlBhlDNnJQqfZaYsWZsYmdVU5dPeeiBmLLzDd0xRCI="
PSK = "u+xLktHw90auwUVtjZEJVpNCbfbFK8GNmGegY/Z0XYs="

PEER = f"""[Peer]
PublicKey = {SERVER_PUBLIC}
PresharedKey = {PSK}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example.com:{{port}}
PersistentKeepalive = 25
"""
# By hand from upstream server_scripts/awg_legacy/template.conf: no S3/S4, empty I1-I5 dropped.
EXPECTED_LEGACY = f"""[Interface]
Address = 10.8.1.3/32
DNS = $PRIMARY_DNS, $SECONDARY_DNS
PrivateKey = {CLIENT_KEYS.private_key}
Jc = 4
Jmin = 10
Jmax = 50
S1 = 72
S2 = 56
H1 = 1020325451
H2 = 1183243451
H3 = 1451245281
H4 = 1785210913

{PEER.format(port=36715)}"""
# From server_scripts/wireguard/template.conf.
EXPECTED_WIREGUARD = f"""[Interface]
Address = 10.8.1.3/32
DNS = $PRIMARY_DNS, $SECONDARY_DNS
PrivateKey = {CLIENT_KEYS.private_key}

{PEER.format(port=51820)}"""


def profile_for(text: str, container: WgContainer) -> AwgClientProfile:
    return AwgClientProfile(
        host="vpn.example.com",
        server=AwgServerSettings.from_config(AwgServerConfig.parse(text)),
        server_public_key=SERVER_PUBLIC,
        preshared_key=PSK,
        keys=CLIENT_KEYS,
        client_ip=IPv4Address("10.8.1.3"),
        container=container,
    )


def test_legacy_native_config_follows_its_template(read_fixture: Callable[[str], str]) -> None:
    profile = profile_for(read_fixture("awg_legacy_wg0.conf"), AWG_LEGACY)

    assert profile.native_config() == EXPECTED_LEGACY


def test_legacy_vpn_config_is_an_awg_container(read_fixture: Callable[[str], str]) -> None:
    profile = profile_for(read_fixture("awg_legacy_wg0.conf"), AWG_LEGACY)

    config = profile.vpn_config()
    awg = config["containers"][0]["awg"]

    assert config["containers"][0]["container"] == "amnezia-awg"
    assert config["defaultContainer"] == "amnezia-awg"
    assert awg["port"] == "36715"
    assert awg["H1"] == "1020325451"
    assert "S3" not in awg
    assert "protocol_version" not in awg
    client = json.loads(awg["last_config"])
    assert client["S2"] == "56"
    assert client["persistent_keep_alive"] == "25"


def test_wireguard_native_config_has_no_awg_parameters(read_fixture: Callable[[str], str]) -> None:
    profile = profile_for(read_fixture("wireguard_wg0.conf"), WIREGUARD)

    assert profile.native_config() == EXPECTED_WIREGUARD
    assert profile.conf_file(("9.9.9.9", "149.112.112.112")).startswith(
        "[Interface]\nAddress = 10.8.1.3/32\nDNS = 9.9.9.9, 149.112.112.112\n"
    )


def test_wireguard_vpn_config_matches_wireguard_protocol_config(
    read_fixture: Callable[[str], str],
) -> None:
    profile = profile_for(read_fixture("wireguard_wg0.conf"), WIREGUARD)

    config = profile.vpn_config("Home")
    wireguard = config["containers"][0]["wireguard"]
    last_config = json.loads(wireguard.pop("last_config"))

    assert config == {
        "format_version": 1,
        "description": "Home",
        "hostName": "vpn.example.com",
        "containers": [{"container": "amnezia-wireguard", "wireguard": wireguard}],
        "defaultContainer": "amnezia-wireguard",
        "dns1": "1.1.1.1",
        "dns2": "1.0.0.1",
    }
    assert wireguard == {"port": "51820", "transport_proto": "udp", "subnet_address": "10.8.1.0"}
    assert last_config == {
        "config": EXPECTED_WIREGUARD,
        "hostName": "vpn.example.com",
        "port": 51820,
        "client_ip": "10.8.1.3",
        "client_priv_key": CLIENT_KEYS.private_key,
        "client_pub_key": CLIENT_KEYS.public_key,
        "server_pub_key": SERVER_PUBLIC,
        "psk_key": PSK,
        "clientId": CLIENT_KEYS.public_key,
        "allowed_ips": ["0.0.0.0/0", "::/0"],
        "persistent_keep_alive": "25",
        "mtu": "1376",
    }


def test_container_files_follow_upstream_constants() -> None:
    assert AWG_LEGACY.config_path == "/opt/amnezia/awg/wg0.conf"
    assert AWG_LEGACY.clients_table_path == "/opt/amnezia/awg/clientsTable"
    assert (AWG_LEGACY.tool, AWG_LEGACY.interface) == ("wg", "wg0")
    assert WIREGUARD.config_path == "/opt/amnezia/wireguard/wg0.conf"
    assert WIREGUARD.psk_path == "/opt/amnezia/wireguard/wireguard_psk.key"
    assert WIREGUARD.clients_table_path == "/opt/amnezia/wireguard/clientsTable"
