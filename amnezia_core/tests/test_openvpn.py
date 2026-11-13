import json
from collections.abc import Callable
from datetime import datetime
from ipaddress import IPv4Network

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID

from amnezia_core.errors import ConfigFormatError, InvalidKeyError
from amnezia_core.openvpn import (
    CertificateIndex,
    ClientKey,
    OpenVpnClientProfile,
    OpenVpnSettings,
    ca_fingerprint,
    certificate_serial,
    is_valid_client_id,
    new_client_id,
)
from amnezia_core.tests.pki import FakeCa
from amnezia_core.vpn_link import decode_vpn_link, encode_vpn_link

CLIENT = "Kq3pX9tZ0bLmN4vR7sW2yA8cE6dF1gH5"
CA = "-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n"
CERT = "Certificate:\n-----BEGIN CERTIFICATE-----\nCLIENT\n-----END CERTIFICATE-----\n"
KEY = "-----BEGIN PRIVATE KEY-----\nKEY\n-----END PRIVATE KEY-----\n"
TA = (
    "#\n# 2048 bit OpenVPN static key\n#\n"
    "-----BEGIN OpenVPN Static key V1-----\nTA\n-----END OpenVPN Static key V1-----"
)

# Written by hand from template.ovpn and OpenVpnConfigurator::createConfig, default install.
EXPECTED_NATIVE = f"""\
client
dev tun
proto udp
resolv-retry infinite
nobind
persist-key
persist-tun

cipher AES-256-GCM
auth SHA512
verb 3
tls-client
tls-version-min 1.2
key-direction 1
remote-cert-tls server
redirect-gateway def1 bypass-dhcp

dhcp-option DNS $PRIMARY_DNS
dhcp-option DNS $SECONDARY_DNS


remote vpn.example.com 1194



<ca>
{CA}
</ca>
<cert>
{CERT}
</cert>
<key>
{KEY}
</key>
<tls-auth>
-----BEGIN OpenVPN Static key V1-----
TA
-----END OpenVPN Static key V1-----

</tls-auth>
"""


@pytest.fixture
def server_text(read_fixture: Callable[[str], str]) -> str:
    return read_fixture("openvpn_server.conf")


@pytest.fixture
def index(read_fixture: Callable[[str], str]) -> CertificateIndex:
    return CertificateIndex.parse(read_fixture("openvpn_index.txt"))


@pytest.fixture
def profile() -> OpenVpnClientProfile:
    return OpenVpnClientProfile("vpn.example.com", OpenVpnSettings(), CLIENT, KEY, CA, CERT, TA)


def test_reads_the_server_config_of_the_default_install(server_text: str) -> None:
    assert OpenVpnSettings.from_server_config(server_text) == OpenVpnSettings()


def test_reads_custom_server_settings() -> None:
    text = (
        "port 31194\nproto tcp\nserver 10.9.0.0 255.255.0.0\nncp-disable\n"
        "cipher AES-128-GCM\nauth SHA256\n# tls-auth is off\n"
    )

    settings = OpenVpnSettings.from_server_config(text)

    assert settings == OpenVpnSettings(
        port=31194,
        transport="tcp",
        subnet=IPv4Network("10.9.0.0/16"),
        cipher="AES-128-GCM",
        hash="SHA256",
        ncp_disable=True,
        # The app looks for the word anywhere in the file, comments too.
        tls_auth=True,
    )


@pytest.mark.parametrize(
    "text",
    ["port many\n", "proto sctp\n", "server 10.8.0.x 255.255.255.0\n", "cipher $(reboot)\n"],
)
def test_broken_server_config_is_refused(text: str) -> None:
    with pytest.raises(ConfigFormatError):
        OpenVpnSettings.from_server_config(text)


def test_server_json_carries_only_what_differs_from_upstream_defaults() -> None:
    assert OpenVpnSettings().to_json() == {
        "port": "1194",
        "transport_proto": "udp",
        "subnet_address": "10.8.0.0",
        "subnet_mask": "255.255.255.0",
        "subnet_cidr": "24",
        "cipher": "AES-256-GCM",
        "hash": "SHA512",
    }
    flags = OpenVpnSettings(ncp_disable=True, tls_auth=False).to_json()
    assert (flags["ncp_disable"], flags["tls_auth"]) == (True, False)


@pytest.mark.parametrize(
    "settings",
    [
        OpenVpnSettings(),
        OpenVpnSettings(31194, "tcp", IPv4Network("10.9.0.0/16"), ncp_disable=True, tls_auth=False),
    ],
)
def test_server_json_reads_back(settings: OpenVpnSettings) -> None:
    assert OpenVpnSettings.from_json(settings.to_json()) == settings


def test_script_variables_fill_the_upstream_server_config(server_text: str) -> None:
    variables = OpenVpnSettings().script_variables()

    assert variables["$OPENVPN_TLS_AUTH"] == "tls-auth /opt/amnezia/openvpn/ta.key 0"
    assert variables["$OPENVPN_NCP_DISABLE"] == ""
    assert variables["$OPENVPN_SUBNET_CIDR"] == "24"
    assert "$OPENVPN_TA_KEY" not in variables
    assert OpenVpnSettings(tls_auth=False).script_variables()["$OPENVPN_TA_KEY"] == ""


def test_native_config_follows_the_template(profile: OpenVpnClientProfile) -> None:
    assert profile.native_config() == EXPECTED_NATIVE


def test_native_config_without_tls_auth_drops_the_key_block() -> None:
    profile = OpenVpnClientProfile(
        "1.2.3.4", OpenVpnSettings(tls_auth=False), CLIENT, KEY, CA, CERT, TA
    )

    config = profile.native_config()

    assert "tls-auth" not in config
    assert "TA" not in config
    assert config.endswith(f"<key>\n{KEY}\n</key>\n\n\n\n")


def test_vpn_config_matches_connection_export(profile: OpenVpnClientProfile) -> None:
    config = profile.vpn_config("Amsterdam", ("9.9.9.9", "149.112.112.112"))
    openvpn = config["containers"][0]["openvpn"]
    last_config = openvpn.pop("last_config")

    assert config == {
        "format_version": 1,
        "description": "Amsterdam",
        "hostName": "vpn.example.com",
        "containers": [{"container": "amnezia-openvpn", "openvpn": openvpn}],
        "defaultContainer": "amnezia-openvpn",
        "dns1": "9.9.9.9",
        "dns2": "149.112.112.112",
    }
    assert openvpn == OpenVpnSettings().to_json()
    # Compact JSON with sorted keys, as QJsonDocument writes it.
    assert last_config.startswith('{"block_outside_dns":false,"clientId":"')
    assert json.loads(last_config) == {
        "config": EXPECTED_NATIVE,
        "clientId": CLIENT,
        "block_outside_dns": False,
    }
    openvpn["last_config"] = last_config
    assert decode_vpn_link(encode_vpn_link(config)) == config


def test_ovpn_file_is_the_app_export(profile: OpenVpnClientProfile) -> None:
    ovpn = profile.ovpn_file(("9.9.9.9", "149.112.112.112"))

    assert "$PRIMARY_DNS" not in ovpn
    assert "dhcp-option DNS 9.9.9.9\ndhcp-option DNS 149.112.112.112\n" in ovpn
    assert "redirect-gateway def1 bypass-dhcp" not in ovpn
    assert ovpn.endswith("</tls-auth>\n\nredirect-gateway def1 ipv6 bypass-dhcp\nblock-ipv6\n\n")


def test_ovpn_file_with_amnezia_dns_has_no_second_server(profile: OpenVpnClientProfile) -> None:
    ovpn = profile.ovpn_file(("172.29.172.254", "1.0.0.1"))

    assert "dhcp-option DNS 172.29.172.254\n\n" in ovpn
    assert "1.0.0.1" not in ovpn


def test_repr_hides_the_private_key(profile: OpenVpnClientProfile) -> None:
    assert "KEY" not in repr(profile)
    assert "KEY" not in repr(ClientKey(KEY))


def test_client_ids_are_like_the_app_makes_them() -> None:
    first, second = new_client_id(), new_client_id()

    assert len(first) == 32
    assert first.isalnum()
    assert first != second
    assert is_valid_client_id(first)


@pytest.mark.parametrize("client_id", ["", "AmneziaReq", "a b", "x;reboot", "a/../b", "a" * 65])
def test_unsafe_client_ids_are_refused(client_id: str) -> None:
    assert not is_valid_client_id(client_id)


def test_certificate_request_has_the_client_id_and_signs_with_sha256() -> None:
    key = ClientKey.generate()

    request = x509.load_pem_x509_csr(key.certificate_request(CLIENT).encode())

    assert key.pem.startswith("-----BEGIN PRIVATE KEY-----\n")
    assert request.is_signature_valid
    assert request.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == CLIENT
    assert request.signature_hash_algorithm is not None
    assert request.signature_hash_algorithm.name == "sha256"
    assert request.public_key().key_size == 2048  # type: ignore[union-attr]


def test_certificate_request_refuses_a_bad_key_or_id() -> None:
    with pytest.raises(InvalidKeyError):
        ClientKey("not a key").certificate_request(CLIENT)
    with pytest.raises(InvalidKeyError):
        ClientKey.generate().certificate_request("x;reboot")


def test_serial_and_fingerprint_are_read_through_the_text_dump() -> None:
    ca = FakeCa()
    issued = ca.sign(ClientKey.generate().certificate_request(CLIENT), serial=0xF00D)

    assert certificate_serial(issued) == 0xF00D
    assert len(ca_fingerprint(ca.pem)) == 64
    with pytest.raises(ConfigFormatError):
        certificate_serial("Certificate:\n")


def test_index_lists_the_valid_client_certificates(index: CertificateIndex) -> None:
    assert index.valid_clients() == [CLIENT, "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp"]


def test_hold_and_release_round_trip(
    index: CertificateIndex, read_fixture: Callable[[str], str]
) -> None:
    index.hold(0x0B, datetime(2026, 11, 6, 9, 30, 0))

    assert index.is_held(0x0B)
    assert f"R\t280210120000Z\t261106093000Z\t0B\tunknown\t/CN={CLIENT}\n" in index.serialize()
    assert index.valid_clients() == ["Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp"]
    index.hold(0x0B, datetime(2026, 12, 1))  # held already: the date stays
    assert "261106093000Z" in index.serialize()

    index.release(0x0B)
    index.release(0x0B)

    assert index.serialize() == read_fixture("openvpn_index.txt")


def test_index_refuses_unknown_serials_and_broken_lines(index: CertificateIndex) -> None:
    with pytest.raises(ConfigFormatError):
        index.hold(0x99, datetime(2026, 11, 6))
    with pytest.raises(ConfigFormatError):
        CertificateIndex.parse("V\t280210120000Z\n")
