import json
from collections.abc import Callable
from ipaddress import IPv4Address, IPv4Interface

import pytest

from amnezia_core.awg import AwgServerConfig
from amnezia_core.awg_export import AwgClientProfile, AwgServerSettings, awg_version
from amnezia_core.containers import AWG2, AWG_LEGACY, WIREGUARD, WgContainer
from amnezia_core.errors import ConfigFormatError
from amnezia_core.keys import KeyPair

# RFC 7748 Alice key pair as the client keys.
CLIENT_KEYS = KeyPair.from_private_key("dwdtCnMYpX08FsFyUbJmRd9ML4frwJkqsXf7pR25LCo=")
SERVER_PUBLIC = "WAlBhlDNnJQqfZaYsWZsYmdVU5dPeeiBmLLzDd0xRCI="
PSK = "u+xLktHw90auwUVtjZEJVpNCbfbFK8GNmGegY/Z0XYs="
I1 = (
    "<r 2><b 0x858000010001000000000669636c6f7564"
    "03636f6d0000010001c00c000100010000105a00044d583737>"
)
HEADERS = {
    "H1": "1020325451-1025713284",
    "H2": "1183243451-1193573213",
    "H3": "1451245281-1471212327",
    "H4": "1785210913-1795201230",
}
PARAMS = {"Jc": "4", "Jmin": "10", "Jmax": "50", "S1": "72", "S2": "56", "S3": "32", "S4": "16"}

# Expected values written by hand from upstream template.conf and the *Config::toJson methods.
EXPECTED_NATIVE = f"""[Interface]
Address = 10.8.1.3/32
DNS = $PRIMARY_DNS, $SECONDARY_DNS
PrivateKey = {CLIENT_KEYS.private_key}
Jc = 4
Jmin = 10
Jmax = 50
S1 = 72
S2 = 56
S3 = 32
S4 = 16
H1 = {HEADERS["H1"]}
H2 = {HEADERS["H2"]}
H3 = {HEADERS["H3"]}
H4 = {HEADERS["H4"]}
I1 = {I1}

[Peer]
PublicKey = {SERVER_PUBLIC}
PresharedKey = {PSK}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example.com:55424
PersistentKeepalive = 25
"""


@pytest.fixture
def profile(read_fixture: Callable[[str], str]) -> AwgClientProfile:
    config = AwgServerConfig.parse(read_fixture("awg0.conf"))
    return AwgClientProfile(
        host="vpn.example.com",
        server=AwgServerSettings.from_config(config),
        server_public_key=SERVER_PUBLIC,
        preshared_key=PSK,
        keys=CLIENT_KEYS,
        client_ip=IPv4Address("10.8.1.3"),
    )


def test_native_config_follows_upstream_template(profile: AwgClientProfile) -> None:
    assert profile.native_config() == EXPECTED_NATIVE


def test_conf_file_fills_dns_like_export(profile: AwgClientProfile) -> None:
    conf = profile.conf_file(("9.9.9.9", "149.112.112.112"))

    assert (
        conf
        == EXPECTED_NATIVE.replace("$PRIMARY_DNS, $SECONDARY_DNS", "9.9.9.9, 149.112.112.112")
        + "\n"
    )


def test_client_json_matches_awg_client_config(profile: AwgClientProfile) -> None:
    assert profile.client_json() == {
        "config": EXPECTED_NATIVE,
        "hostName": "vpn.example.com",
        "port": 55424,
        "client_ip": "10.8.1.3",
        "client_priv_key": CLIENT_KEYS.private_key,
        "client_pub_key": CLIENT_KEYS.public_key,
        "server_pub_key": SERVER_PUBLIC,
        "psk_key": PSK,
        "clientId": CLIENT_KEYS.public_key,
        "allowed_ips": ["0.0.0.0/0", "::/0"],
        "persistent_keep_alive": "25",
        "mtu": "1376",
        **PARAMS,
        **HEADERS,
        "I1": I1,
    }


def test_vpn_config_matches_connection_export(profile: AwgClientProfile) -> None:
    config = profile.vpn_config("Amsterdam", ("1.1.1.1", "1.0.0.1"))
    awg = config["containers"][0]["awg"]
    last_config = awg.pop("last_config")

    assert config == {
        "format_version": 1,
        "description": "Amsterdam",
        "hostName": "vpn.example.com",
        "containers": [{"container": "amnezia-awg2", "awg": awg}],
        "defaultContainer": "amnezia-awg2",
        "dns1": "1.1.1.1",
        "dns2": "1.0.0.1",
    }
    assert awg == {
        "port": "55424",
        "transport_proto": "udp",
        "protocol_version": "2",
        "subnet_address": "10.8.1.0",
        "subnet_cidr": "24",
        **PARAMS,
        **HEADERS,
        "I1": I1,
        "I2": "",
        "I3": "",
        "I4": "",
        "I5": "",
    }
    # Qt compact JSON: sorted keys, no spaces.
    assert last_config.startswith('{"H1":"1020325451-1025713284","H2":')
    assert json.loads(last_config) == profile.client_json()


def test_vpn_config_omits_empty_description(profile: AwgClientProfile) -> None:
    assert "description" not in profile.vpn_config()


def test_repr_hides_secrets(profile: AwgClientProfile) -> None:
    assert PSK not in repr(profile)
    assert CLIENT_KEYS.private_key not in repr(profile)


def test_awg3_server_uses_keepalive_range_and_toggles() -> None:
    server = AwgServerSettings(
        51820,
        IPv4Interface("10.8.1.0/24"),
        {"Jc": "4", "RandomTrailers": "off", "DisableCookies": "on", "RekeyTimeout": "5"},
    )
    profile = AwgClientProfile(
        "h", server, SERVER_PUBLIC, PSK, CLIENT_KEYS, IPv4Address("10.8.1.2")
    )
    native = profile.native_config()

    assert server.protocol_version == "3.1"
    assert "PersistentKeepalive = 25-35" in native
    assert "DisableCookies = on" in native
    assert "RandomTrailers" not in native
    assert server.to_json()["RandomTrailers"] == "off"
    assert "RandomTrailers" not in profile.client_json()


@pytest.mark.parametrize(
    ("params", "version"),
    [
        ({"Jc": "4", "H1": "1"}, ""),
        ({"I2": "<r 2>"}, "1.5"),
        ({"H3": "5-9"}, "2"),
        ({"DisableCookies": "OFF"}, ""),
    ],
)
def test_protocol_version_detection(params: dict[str, str], version: str) -> None:
    assert AwgServerSettings(1, IPv4Interface("10.0.0.0/24"), params).protocol_version == version


@pytest.mark.parametrize(
    ("kind", "params", "version"),
    [
        (AWG2, {"HeaderProtectionKey": "key", "S3": "12"}, "3.1"),
        (AWG2, {"S3": "32"}, "2"),
        (AWG2, {"Jc": "4"}, "2"),
        (AWG_LEGACY, {"I1": "<r 2>"}, "1.5"),
        (AWG_LEGACY, {"Jc": "4"}, "1.0"),
        (WIREGUARD, {}, ""),
    ],
)
def test_version_shown_for_a_container(
    kind: WgContainer, params: dict[str, str], version: str
) -> None:
    assert awg_version(kind, params) == version


def test_server_settings_need_port() -> None:
    config = AwgServerConfig.parse("[Interface]\nAddress = 10.8.1.0/24\n")

    with pytest.raises(ConfigFormatError):
        AwgServerSettings.from_config(config)
    assert AwgServerSettings.from_config(config, port=443).port == 443
