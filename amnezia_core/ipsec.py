"""IPsec container of the Amnezia app: the NSS certificate database and libreswan in it.

Mirrors upstream server_scripts/ipsec/configure_container.sh and Ikev2Configurator: the CA,
the server certificate and every client certificate with its key live in the NSS database
sql:/etc/ipsec.d of amnezia-ipsec; libreswan (pluto) accepts any client the CA signed and
the revocation list does not name. A client id is the nickname and the common name of its
certificate.

Here are the parsers of what `certutil`, `crlutil` and `ipsec whack` print, so that the
panel can find clients, revoked serials and the sessions of a client.
"""

import base64
import binascii
import hashlib
import re
import secrets
import string
from dataclasses import dataclass
from datetime import datetime

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from amnezia_core.errors import ConfigFormatError

IPSEC_CONTAINER = "amnezia-ipsec"
# protoToString(Proto::Ikev2): the key of the protocol in the app's server JSON.
IPSEC_PROTOCOL = "ikev2"
# run_container.sh publishes both; they cannot be changed.
IPSEC_PORTS = (500, 4500)
NSS_DATABASE = "sql:/etc/ipsec.d"
CA_NICKNAME = "IKEv2 VPN CA"
IKEV2_CONFIG_PATH = "/etc/ipsec.d/ikev2.conf"
# The CA certificate as base64 of DER: `certutil -a` without the BEGIN and END lines.
IPSEC_CA_CERT_PATH = "/etc/ipsec.d/ca_cert_base64.p12"
IPSEC_DIR = "/opt/amnezia/ikev2"
IPSEC_CLIENTS_DIR = f"{IPSEC_DIR}/clients"
# DESIGN 3.2: the app keeps no client list for IPsec; the panel keeps one where the others are.
IPSEC_CLIENTS_TABLE_PATH = f"{IPSEC_DIR}/clientsTable"
CONNECTION_NAME = "ikev2-cp"
# rightaddresspool of ikev2.conf.
CLIENT_SUBNET = "192.168.43.0/24"

# genBaseVars for amnezia-ipsec: the fixed networks of the container. The app installs it
# without DNS settings, so clients get the default servers of genBaseVars.
_SCRIPT_VARIABLES = {
    "$IPSEC_VPN_L2TP_NET": "192.168.42.0/24",
    "$IPSEC_VPN_L2TP_POOL": "192.168.42.10-192.168.42.250",
    "$IPSEC_VPN_L2TP_LOCAL": "192.168.42.1",
    "$IPSEC_VPN_XAUTH_NET": CLIENT_SUBNET,
    "$IPSEC_VPN_XAUTH_POOL": "192.168.43.10-192.168.43.250",
    "$IPSEC_VPN_SHA2_TRUNCBUG": "yes",
    "$IPSEC_VPN_VPN_ANDROID_MTU_FIX": "yes",
    "$IPSEC_VPN_DISABLE_IKEV2": "no",
    "$IPSEC_VPN_DISABLE_L2TP": "no",
    "$IPSEC_VPN_DISABLE_XAUTH": "no",
    "$IPSEC_VPN_C2C_TRAFFIC": "no",
    "$PRIMARY_SERVER_DNS": "8.8.8.8",
    "$SECONDARY_SERVER_DNS": "8.8.4.4",
}
_CLIENT_ID_LENGTH = 16
_CLIENT_ID_ALPHABET = string.ascii_letters + string.digits
# Ids that are safe in certutil commands and file names; the app makes 16 letters and digits.
# Neither the CA nickname (spaces) nor the server one (an IP address, dots) can match.
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LEFT_ID = re.compile(r"^\s*leftid=(\S+)\s*$", re.MULTILINE)
# The lines of ikev2.conf that name the server certificate: its nickname and its identity.
_SERVER_LINES = re.compile(r"^(\s*left(?:id|cert)=)\S+(\s*)$", re.MULTILINE)
# `certutil -L`: the nickname, two spaces or more, the trust flags `SSL,S/MIME,JAR/XPI`.
_LISTED_CERTIFICATE = re.compile(r"^(\S.*?)\s{2,}(\S*,\S*,\S*)\s*$")
# SECU_PrintInteger: a short serial is `Serial Number: 123 (0x7b)`, a long one hex bytes below.
_SHORT_SERIAL = re.compile(r"Serial Number:\s*(\d+)\s*\(0x[0-9a-fA-F]+\)")
_SERIAL_LABEL = re.compile(r"Serial Number:\s*$")
_HEX_BYTES = re.compile(r"^\s*([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2})*:?)\s*$")
# `ipsec whack --trafficstatus` and `--showstates`: `#3: "ikev2-cp"[2] 203.0.113.5...`.
_STATE = re.compile(r'#(\d+): "([^"]+)"(\[\d+\])?')
_PEER_ID = re.compile(r"id='([^']*)'")
_CRL_DATE_FORMAT = "%Y%m%d%H%M%SZ"


@dataclass(frozen=True)
class IpsecContainer:
    """The IPsec container of the Amnezia app; its files are at the IPSEC_* paths."""

    name: str = IPSEC_CONTAINER
    protocol: str = IPSEC_PROTOCOL


IPSEC = IpsecContainer()


def script_variables(server_ip: str) -> dict[str, str]:
    """Values for the placeholders of configure_container.sh; `server_ip` is the identity."""
    return {**_SCRIPT_VARIABLES, "$SERVER_IP_ADDRESS": server_ip}


def new_client_id() -> str:
    """Utils::getRandomString(16): the nickname and common name of a new client certificate."""
    return "".join(secrets.choice(_CLIENT_ID_ALPHABET) for _ in range(_CLIENT_ID_LENGTH))


def is_valid_client_id(client_id: str) -> bool:
    """An id the panel may put into certutil commands and file names."""
    return bool(_CLIENT_ID.match(client_id))


def export_path(client_id: str) -> str:
    """Where Ikev2Configurator exports the PKCS#12 of a client."""
    return f"{IPSEC_CLIENTS_DIR}/{client_id}.p12"


def server_identity(ikev2_conf: str) -> str:
    """`leftid` of ikev2.conf: the server IP the server certificate is issued for."""
    match = _LEFT_ID.search(ikev2_conf)
    if match is None:
        raise ConfigFormatError("ikev2.conf has no leftid.")
    return match[1]


def with_server_identity(ikev2_conf: str, server_ip: str) -> str:
    """ikev2.conf with `leftcert` and `leftid` naming the certificate issued for `server_ip`."""
    server_identity(ikev2_conf)
    return _SERVER_LINES.sub(lambda match: f"{match[1]}{server_ip}{match[2]}", ikev2_conf)


def server_certificate_options(server_ip: str) -> list[str]:
    """certutil options of configure_container.sh for the server certificate the CA signs.

    The nickname, the common name and the SAN are the server IP; `-z` with the noise is
    added by the caller.
    """
    return [
        "-S", "-c", CA_NICKNAME, "-n", server_ip,
        "-s", f"O=IKEv2 VPN,CN={server_ip}", "-k", "rsa", "-g", "3072", "-v", "120",
        "-d", NSS_DATABASE, "-t", ",,",
        "--keyUsage", "digitalSignature,keyEncipherment",
        "--extKeyUsage", "serverAuth",
        "--extSAN", f"ip:{server_ip},dns:{server_ip}",
    ]  # fmt: skip


def listed_clients(certutil_listing: str) -> list[str]:
    """Client nicknames in `certutil -L`, in listing order; the CA and the server are left out."""
    clients = []
    for line in certutil_listing.splitlines():
        match = _LISTED_CERTIFICATE.match(line)
        if match and is_valid_client_id(match[1]) and match[1] not in clients:
            clients.append(match[1])
    return clients


def pem_serial(certutil_ascii: str) -> int:
    """Serial of the certificate `certutil -L -n <id> -a` printed."""
    start = certutil_ascii.find("-----BEGIN CERTIFICATE-----")
    if start < 0:
        raise ConfigFormatError("certutil printed no PEM certificate.")
    try:
        return x509.load_pem_x509_certificate(certutil_ascii[start:].encode()).serial_number
    except ValueError as error:
        raise ConfigFormatError("The client certificate cannot be read.") from error


def revoked_serials(crlutil_listing: str) -> set[int]:
    """Serials of the CRL `crlutil -L` printed; no CRL yet prints nothing."""
    serials: set[int] = set()
    hex_digits: list[str] | None = None
    for line in crlutil_listing.splitlines():
        if hex_digits is not None:
            match = _HEX_BYTES.match(line)
            if match:
                hex_digits.append(match[1].replace(":", ""))
                continue
            if hex_digits:
                serials.add(int("".join(hex_digits), 16))
            hex_digits = None
        short = _SHORT_SERIAL.search(line)
        if short:
            serials.add(int(short[1]))
        elif _SERIAL_LABEL.search(line):
            hex_digits = []
    if hex_digits:
        serials.add(int("".join(hex_digits), 16))
    return serials


def crl_entry(serial: int, when: datetime) -> str:
    """A `crlutil -M` line that puts the certificate into the CRL."""
    return f"addcert {serial} {when.strftime(_CRL_DATE_FORMAT)}\n"


def crl_removal(serial: int) -> str:
    """A `crlutil -M` line that takes the certificate out of the CRL."""
    return f"rmcert {serial}\n"


def client_states(traffic_status: str, states: str, client_id: str) -> list[int]:
    """States of `ipsec whack` that carry sessions of the client, newest first.

    `--trafficstatus` names the peer of every tunnel; `--showstates` adds the IKE states of
    the same connection instances, so that deleting them all ends the sessions.
    """
    own_cn = re.compile(rf"(?:^|[,/]\s*)CN={re.escape(client_id)}(?:$|[,/])")
    instances: set[str] = set()
    found: set[int] = set()
    for line in traffic_status.splitlines():
        state, peer = _STATE.search(line), _PEER_ID.search(line)
        if state and peer and own_cn.search(peer[1]):
            found.add(int(state[1]))
            if state[3]:  # a template connection has an instance per peer
                instances.add(state[2] + state[3])
    for line in states.splitlines():
        state = _STATE.search(line)
        if state and state[3] and state[2] + state[3] in instances:
            found.add(int(state[1]))
    return sorted(found, reverse=True)


def ca_certificate(ca_base64: str) -> x509.Certificate:
    """The CA of ca_cert_base64.p12: base64 lines of DER."""
    try:
        der = base64.b64decode("".join(ca_base64.split()), validate=True)
        return x509.load_der_x509_certificate(der)
    except (binascii.Error, ValueError) as error:
        raise ConfigFormatError("ca_cert_base64.p12 is not a base64 certificate.") from error


def ca_fingerprint(ca_base64: str) -> str:
    """SHA-256 of the CA certificate: what identifies the server PKI without a secret."""
    der = ca_certificate(ca_base64).public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()
