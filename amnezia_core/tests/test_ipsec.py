import base64
import json
import plistlib
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization

from amnezia_core.errors import ConfigFormatError
from amnezia_core.ipsec import (
    ca_fingerprint,
    client_states,
    crl_entry,
    crl_removal,
    is_valid_client_id,
    listed_clients,
    new_client_id,
    pem_serial,
    revoked_serials,
    server_certificate_options,
    server_identity,
    with_server_identity,
)
from amnezia_core.ipsec_profile import IpsecClientProfile
from amnezia_core.tests.pki import FakeCa
from amnezia_core.vpn_link import decode_vpn_link, encode_vpn_link

CLIENT = "AbCdEf0123456789"

IKEV2_CONF = """\
conn ikev2-cp
  left=%defaultroute
  leftcert=203.0.113.7
  leftid=203.0.113.7
  rightaddresspool=192.168.43.10-192.168.43.250
"""

CERTUTIL_LISTING = """
Certificate Nickname                                         Trust Attributes
                                                             SSL,S/MIME,JAR/XPI

IKEv2 VPN CA                                                 CTu,u,u
203.0.113.7                                                  u,u,u
AbCdEf0123456789                                             u,u,u
bad name here                                                u,u,u
Zz9                                                          ,,
"""

CRL_LISTING = """\
CRL Info:
:
    Version: 2 (0x1)
    Issuer: "CN=IKEv2 VPN CA,O=IKEv2 VPN"
    This Update: Fri Nov 06 10:00:00 2026
    Entry 1 (0x1):
        Serial Number: 1234567890 (0x499602d2)
        Revocation Date: Fri Nov 06 10:00:00 2026
    Entry 2 (0x2):
        Serial Number:
            00:b2:3c:4d:5e:6f:70:81:92:a3:
            b4
        Revocation Date: Fri Nov 06 10:00:00 2026
"""

TRAFFIC_STATUS = """\
006 #4: "ikev2-cp"[2] 198.51.100.5, type=ESP, add_time=1700000000, inBytes=1, outBytes=2, \
id='O=IKEv2 VPN, CN=AbCdEf0123456789', lease=192.168.43.10/32
006 #6: "ikev2-cp"[3] 198.51.100.9, type=ESP, add_time=1700000000, inBytes=1, outBytes=2, \
id='O=IKEv2 VPN, CN=AbCdEf01234567890', lease=192.168.43.11/32
"""

SHOW_STATES = """\
000 #3: "ikev2-cp"[2] 198.51.100.5:4500 STATE_V2_ESTABLISHED_IKE_SA (established IKE SA)
000 #4: "ikev2-cp"[2] 198.51.100.5:4500 STATE_V2_ESTABLISHED_CHILD_SA (established Child SA)
000 #5: "ikev2-cp"[3] 198.51.100.9:4500 STATE_V2_ESTABLISHED_IKE_SA (established IKE SA)
"""


def _ca_base64(ca: FakeCa) -> str:
    der = ca.certificate.public_bytes(serialization.Encoding.DER)
    text = base64.b64encode(der).decode()
    return "\n".join(text[i : i + 64] for i in range(0, len(text), 64)) + "\n"


@pytest.fixture
def profile() -> IpsecClientProfile:
    p12 = base64.b64encode(b"\x30\x82pkcs12 of the client").decode()
    return IpsecClientProfile("vpn.example.com", "203.0.113.7", CLIENT, p12, _ca_base64(FakeCa()))


def test_new_client_id_is_like_the_app() -> None:
    client_id = new_client_id()
    assert len(client_id) == 16
    assert client_id.isalnum()
    assert is_valid_client_id(client_id)


@pytest.mark.parametrize("bad", ["", "IKEv2 VPN CA", "203.0.113.7", "a;rm -rf", "x" * 65])
def test_ids_unsafe_in_commands_are_refused(bad: str) -> None:
    assert not is_valid_client_id(bad)


def test_server_identity_is_leftid() -> None:
    assert server_identity(IKEV2_CONF) == "203.0.113.7"
    with pytest.raises(ConfigFormatError):
        server_identity("conn ikev2-cp\n")


def test_with_server_identity_renames_the_server_certificate() -> None:
    conf = with_server_identity(IKEV2_CONF, "198.51.100.9")
    assert "leftcert=198.51.100.9\n" in conf and "leftid=198.51.100.9\n" in conf
    assert "203.0.113.7" not in conf
    assert conf.replace("198.51.100.9", "203.0.113.7") == IKEV2_CONF
    with pytest.raises(ConfigFormatError):
        with_server_identity("conn ikev2-cp\n", "198.51.100.9")


def test_server_certificate_options_follow_configure_container() -> None:
    options = server_certificate_options("198.51.100.9")
    assert options[options.index("-c") + 1] == "IKEv2 VPN CA"
    assert options[options.index("-n") + 1] == "198.51.100.9"
    assert "O=IKEv2 VPN,CN=198.51.100.9" in options
    assert "ip:198.51.100.9,dns:198.51.100.9" in options
    assert options[options.index("--extKeyUsage") + 1] == "serverAuth"


def test_listed_clients_skip_the_ca_and_the_server() -> None:
    assert listed_clients(CERTUTIL_LISTING) == [CLIENT, "Zz9"]


def test_pem_serial_reads_certutil_ascii() -> None:
    ca = FakeCa()
    pem = ca.certificate.public_bytes(serialization.Encoding.PEM).decode()
    assert pem_serial(pem) == 1
    with pytest.raises(ConfigFormatError):
        pem_serial("certutil: Could not find cert")


def test_revoked_serials_short_and_long() -> None:
    assert revoked_serials(CRL_LISTING) == {1234567890, 0xB23C4D5E6F708192A3B4}
    assert revoked_serials("") == set()


def test_crl_script_lines() -> None:
    when = datetime(2026, 11, 6, 10, 0, 5, tzinfo=UTC)
    assert crl_entry(1234, when) == "addcert 1234 20261106100005Z\n"
    assert crl_removal(1234) == "rmcert 1234\n"


def test_client_states_are_only_its_own() -> None:
    assert client_states(TRAFFIC_STATUS, SHOW_STATES, CLIENT) == [4, 3]
    assert client_states("", SHOW_STATES, CLIENT) == []


def test_ca_fingerprint_reads_base64_lines() -> None:
    ca = FakeCa()
    assert len(ca_fingerprint(_ca_base64(ca))) == 64
    with pytest.raises(ConfigFormatError):
        ca_fingerprint("not base64!")


def test_native_config_is_genikev2config(profile: IpsecClientProfile) -> None:
    native = profile.native_config()
    assert native.startswith('{\n    "cert": "')
    assert json.loads(native) == {
        "cert": profile.p12,
        "hostName": "vpn.example.com",
        "password": "",
        "userName": CLIENT,
    }


def test_vpn_config_is_the_apps_server_json(profile: IpsecClientProfile) -> None:
    config = decode_vpn_link(encode_vpn_link(profile.vpn_config("Key", ("9.9.9.9", "1.1.1.1"))))
    assert config["hostName"] == "vpn.example.com"
    assert config["defaultContainer"] == "amnezia-ipsec"
    container = config["containers"][0]
    assert container["container"] == "amnezia-ipsec"
    ikev2 = container["ikev2"]
    assert ikev2["hostName"] == "vpn.example.com"
    client = json.loads(ikev2["last_config"])
    # Ikev2ClientConfig::toJson leaves the empty password out.
    assert set(client) == {"config", "hostName", "userName", "cert", "clientId"}
    assert client["clientId"] == client["userName"] == CLIENT
    assert client["config"] == profile.native_config()


def test_sswan_is_the_strongswan_template(profile: IpsecClientProfile) -> None:
    sswan = json.loads(profile.sswan_file())
    assert sswan["type"] == "ikev2-cert"
    assert sswan["name"] == "IKEv2 VPN (203.0.113.7)"
    assert sswan["remote"] == {"addr": "203.0.113.7"}
    assert sswan["local"] == {"p12": profile.p12, "rsa-pss": "true"}
    assert sswan["uuid"] == json.loads(profile.sswan_file())["uuid"]  # the same every time


def test_mobileconfig_is_a_valid_profile(profile: IpsecClientProfile) -> None:
    text = profile.mobileconfig_file()
    assert "$" not in text
    plist = plistlib.loads(text.encode())
    vpn, p12, ca = plist["PayloadContent"]
    assert vpn["IKEv2"]["LocalIdentifier"] == CLIENT
    assert vpn["IKEv2"]["RemoteAddress"] == vpn["IKEv2"]["RemoteIdentifier"] == "203.0.113.7"
    assert vpn["IKEv2"]["PayloadCertificateUUID"] == p12["PayloadUUID"]
    assert p12["PayloadContent"] == base64.b64decode(profile.p12)
    assert p12["Password"] == ""
    # The CA is DER, not base64 of base64 as upstream genMobileConfig makes it.
    assert ca["PayloadContent"] == base64.b64decode("".join(profile.ca_cert.split()))
    uuids = [plist["PayloadUUID"], vpn["PayloadUUID"], p12["PayloadUUID"], ca["PayloadUUID"]]
    assert len(set(uuids)) == len(uuids)
    assert text == profile.mobileconfig_file()


def test_repr_hides_the_credential(profile: IpsecClientProfile) -> None:
    assert profile.p12 not in repr(profile)
