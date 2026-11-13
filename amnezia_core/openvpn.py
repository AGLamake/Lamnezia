"""OpenVPN container of the Amnezia app: server.conf, client certificates, .ovpn and vpn://.

Mirrors upstream OpenVpnServerConfig and OpenVpnClientConfig (openVpnProtocolConfig.cpp),
OpenVpnConfigurator, OpenVpnInstaller::extractConfigFromContainer, genOpenVpnVars and
ExportController::generateOpenVpnConfig.

A client is an RSA key and a certificate signed by the easy-rsa CA of the container; its id is
the certificate's common name. Disabling a client revokes its certificate in pki/index.txt
without deleting it, so enabling it again brings the same config back to life.
"""

import hashlib
import re
import secrets
import string
from dataclasses import dataclass, replace
from datetime import datetime
from ipaddress import AddressValueError, IPv4Address, IPv4Network, NetmaskValueError
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from amnezia_core.errors import ConfigFormatError, InvalidKeyError
from amnezia_core.qt_json import to_qt_compact_json
from amnezia_core.script_vars import substitute
from amnezia_core.vpn_link import DEFAULT_DNS, connection_config

OPENVPN_CONTAINER = "amnezia-openvpn"
OPENVPN_PROTOCOL = "openvpn"
OPENVPN_DIR = "/opt/amnezia/openvpn"
OPENVPN_CONFIG_PATH = f"{OPENVPN_DIR}/server.conf"
OPENVPN_CLIENTS_TABLE_PATH = f"{OPENVPN_DIR}/clientsTable"
OPENVPN_CA_CERT_PATH = f"{OPENVPN_DIR}/pki/ca.crt"
OPENVPN_TA_KEY_PATH = f"{OPENVPN_DIR}/ta.key"
OPENVPN_INDEX_PATH = f"{OPENVPN_DIR}/pki/index.txt"
# Where the app uploads certificate requests; easy-rsa copies them to pki/reqs on import.
OPENVPN_REQUESTS_DIR = f"{OPENVPN_DIR}/clients"
# Certificate of the server itself, signed by the same CA; never a client.
SERVER_CERT_NAME = "AmneziaReq"

DEFAULT_PORT = 1194
DEFAULT_TRANSPORT = "udp"
TRANSPORTS = ("udp", "tcp")
DEFAULT_SUBNET = IPv4Network("10.8.0.0/24")
DEFAULT_CIPHER = "AES-256-GCM"
DEFAULT_HASH = "SHA512"
# The choices of the OpenVPN settings page of the app.
CIPHERS = (
    "AES-256-GCM", "AES-192-GCM", "AES-128-GCM", "AES-256-CBC", "AES-192-CBC", "AES-128-CBC",
    "ChaCha20-Poly1305", "ARIA-256-CBC", "CAMELLIA-256-CBC", "none",
)  # fmt: skip
HASHES = (
    "SHA512", "SHA384", "SHA256", "SHA3-512", "SHA3-384", "SHA3-256", "whirlpool",
    "BLAKE2b512", "BLAKE2s256", "SHA1",
)  # fmt: skip
# protocols::dns::amneziaDnsIp: the AmneziaDNS container; it needs no second server.
AMNEZIA_DNS_IP = "172.29.172.254"

_NCP_DISABLE = "ncp-disable"
_TLS_AUTH = f"tls-auth {OPENVPN_TA_KEY_PATH} 0"
# The last line of server.conf before $OPENVPN_TLS_AUTH and the extra server config.
_LAST_FIXED_LINE = "tls-version-min"
_CLIENT_ID_LENGTH = 32
_CLIENT_ID_ALPHABET = string.ascii_letters + string.digits
# Ids that are safe in easy-rsa commands and file names; the app makes 32 letters and digits.
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RSA_BITS = 2048
_RSA_EXPONENT = 65537
_CIPHER_OR_HASH = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_REVOKED_AT_FORMAT = "%y%m%d%H%M%SZ"

# Verbatim copy of amnezia-client client/server_scripts/openvpn/template.ovpn.
_CLIENT_TEMPLATE = """\
client
dev tun
proto $OPENVPN_TRANSPORT_PROTO
resolv-retry infinite
nobind
persist-key
persist-tun
$OPENVPN_NCP_DISABLE
cipher $OPENVPN_CIPHER
auth $OPENVPN_HASH
verb 3
tls-client
tls-version-min 1.2
key-direction 1
remote-cert-tls server
redirect-gateway def1 bypass-dhcp

dhcp-option DNS $PRIMARY_DNS
dhcp-option DNS $SECONDARY_DNS
block-outside-dns

remote $REMOTE_HOST $OPENVPN_PORT

$OPENVPN_ADDITIONAL_CLIENT_CONFIG

<ca>
$OPENVPN_CA_CERT
</ca>
<cert>
$OPENVPN_CLIENT_CERT
</cert>
<key>
$OPENVPN_PRIV_KEY
</key>
<tls-auth>
$OPENVPN_TA_KEY
</tls-auth>
"""


@dataclass(frozen=True)
class OpenVpnContainer:
    """The OpenVPN container of the Amnezia app; its files are at the OPENVPN_* paths."""

    name: str = OPENVPN_CONTAINER
    protocol: str = OPENVPN_PROTOCOL


OPENVPN = OpenVpnContainer()


def new_client_id() -> str:
    """Utils::getRandomString(32): the common name of a new client certificate."""
    return "".join(secrets.choice(_CLIENT_ID_ALPHABET) for _ in range(_CLIENT_ID_LENGTH))


def is_valid_client_id(client_id: str) -> bool:
    """An id the panel may put into easy-rsa commands and file names."""
    return bool(_CLIENT_ID.match(client_id)) and client_id != SERVER_CERT_NAME


def certificate_path(client_id: str) -> str:
    return f"{OPENVPN_DIR}/pki/issued/{client_id}.crt"


def request_path(client_id: str) -> str:
    """The request after `easyrsa import-req`; its presence means the import is done."""
    return f"{OPENVPN_DIR}/pki/reqs/{client_id}.req"


def upload_path(client_id: str) -> str:
    return f"{OPENVPN_REQUESTS_DIR}/{client_id}.req"


@dataclass(frozen=True, repr=False)
class ClientKey:
    """RSA key of a client in PKCS#8 PEM, as the app writes it; `repr` hides it."""

    pem: str

    def __repr__(self) -> str:
        return "ClientKey(...)"

    @classmethod
    def generate(cls) -> "ClientKey":
        key = rsa.generate_private_key(public_exponent=_RSA_EXPONENT, key_size=_RSA_BITS)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return cls(pem.decode())

    def certificate_request(self, client_id: str) -> str:
        """CSR signed with SHA-256 for `client_id`.

        The app also puts C=ORG and an empty O into the subject; easy-rsa signs with the CN
        alone, so the certificate is the same. A country of three letters is not valid X.509
        and is left out here.
        """
        if not is_valid_client_id(client_id):
            raise InvalidKeyError(f"Not a valid OpenVPN client id: {client_id!r}")
        try:
            key = serialization.load_pem_private_key(self.pem.encode(), password=None)
        except ValueError as error:
            raise InvalidKeyError("The OpenVPN client key is not a PEM private key.") from error
        if not isinstance(key, rsa.RSAPrivateKey):
            raise InvalidKeyError("The OpenVPN client key is not an RSA key.")
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, client_id)])
        request = (
            x509.CertificateSigningRequestBuilder().subject_name(subject).sign(key, hashes.SHA256())
        )
        return request.public_bytes(serialization.Encoding.PEM).decode()


def certificate_serial(text: str) -> int:
    """Serial of a PEM certificate; easy-rsa puts a text dump before the PEM block."""
    start = text.find("-----BEGIN CERTIFICATE-----")
    if start < 0:
        raise ConfigFormatError("The file holds no PEM certificate.")
    try:
        return x509.load_pem_x509_certificate(text[start:].encode()).serial_number
    except ValueError as error:
        raise ConfigFormatError("The certificate cannot be read.") from error


def ca_fingerprint(text: str) -> str:
    """SHA-256 of the CA certificate: what identifies the server PKI without a secret."""
    start = text.find("-----BEGIN CERTIFICATE-----")
    if start < 0:
        raise ConfigFormatError("ca.crt holds no PEM certificate.")
    try:
        certificate = x509.load_pem_x509_certificate(text[start:].encode())
    except ValueError as error:
        raise ConfigFormatError("ca.crt cannot be read.") from error
    return hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()


@dataclass(frozen=True)
class OpenVpnSettings:
    """Server half of a profile: upstream OpenVpnServerConfig with the defaults filled in."""

    port: int = DEFAULT_PORT
    transport: str = DEFAULT_TRANSPORT
    subnet: IPv4Network = DEFAULT_SUBNET
    cipher: str = DEFAULT_CIPHER
    hash: str = DEFAULT_HASH
    ncp_disable: bool = False
    tls_auth: bool = True
    additional_client_config: str = ""
    additional_server_config: str = ""

    def __post_init__(self) -> None:
        if self.transport not in TRANSPORTS:
            raise ConfigFormatError(f"OpenVPN transport {self.transport!r} is not supported.")
        if not 0 < self.port < 2**16:
            raise ConfigFormatError(f"OpenVPN port {self.port} is out of range.")
        for value in (self.cipher, self.hash):
            if not _CIPHER_OR_HASH.match(value):
                raise ConfigFormatError(f"Not a valid OpenVPN cipher or hash: {value!r}")

    @classmethod
    def from_server_config(cls, text: str) -> "OpenVpnSettings":
        """server.conf as extractConfigFromContainer reads it; port and proto are read too."""
        values: dict[str, str] = {}
        for line in text.split("\n"):
            parts = line.strip().split(" ")
            if parts[0].startswith("#") or len(parts) < 2:  # noqa: PLR2004 - key and value
                continue
            values[parts[0]] = " ".join(parts[1:])
        try:
            return cls(
                port=int(values.get("port", DEFAULT_PORT)),
                transport=values.get("proto", DEFAULT_TRANSPORT),
                subnet=_subnet(values.get("server", "")),
                cipher=values.get("cipher") or DEFAULT_CIPHER,
                hash=values.get("auth") or DEFAULT_HASH,
                ncp_disable=_NCP_DISABLE in text,
                tls_auth="tls-auth" in text,
                additional_server_config=_additional_server_config(text),
            )
        except ValueError as error:
            raise ConfigFormatError(f"server.conf cannot be read: {error}") from error

    def to_json(self) -> dict[str, Any]:
        """OpenVpnServerConfig::toJson: flags only when they differ from the defaults."""
        data: dict[str, Any] = {
            "port": str(self.port),
            "transport_proto": self.transport,
            "subnet_address": str(self.subnet.network_address),
            "subnet_mask": str(self.subnet.netmask),
            "subnet_cidr": str(self.subnet.prefixlen),
            "cipher": self.cipher,
            "hash": self.hash,
            "additional_client_config": self.additional_client_config,
            "additional_server_config": self.additional_server_config,
        }
        if self.ncp_disable:
            data["ncp_disable"] = True
        if not self.tls_auth:
            data["tls_auth"] = False
        return {key: value for key, value in data.items() if value != ""}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "OpenVpnSettings":
        """OpenVpnServerConfig::fromJson, the reverse of `to_json`."""
        try:
            return cls(
                port=int(data.get("port", DEFAULT_PORT)),
                transport=data.get("transport_proto", DEFAULT_TRANSPORT),
                subnet=IPv4Network(
                    f"{data.get('subnet_address', DEFAULT_SUBNET.network_address)}"
                    f"/{data.get('subnet_cidr', DEFAULT_SUBNET.prefixlen)}"
                ),
                cipher=data.get("cipher", DEFAULT_CIPHER),
                hash=data.get("hash", DEFAULT_HASH),
                ncp_disable=bool(data.get("ncp_disable", False)),
                tls_auth=bool(data.get("tls_auth", True)),
                additional_client_config=data.get("additional_client_config", ""),
                additional_server_config=data.get("additional_server_config", ""),
            )
        except ValueError as error:
            raise ConfigFormatError(f"OpenVPN settings cannot be read: {error}") from error

    def script_variables(self) -> dict[str, str]:
        """genOpenVpnVars: the `$OPENVPN_*` placeholders of the scripts and the template."""
        variables = {
            "$OPENVPN_SUBNET_IP": str(self.subnet.network_address),
            "$OPENVPN_SUBNET_CIDR": str(self.subnet.prefixlen),
            "$OPENVPN_SUBNET_MASK": str(self.subnet.netmask),
            "$OPENVPN_PORT": str(self.port),
            "$OPENVPN_TRANSPORT_PROTO": self.transport,
            "$OPENVPN_NCP_DISABLE": _NCP_DISABLE if self.ncp_disable else "",
            "$OPENVPN_CIPHER": self.cipher,
            "$OPENVPN_HASH": self.hash,
            "$OPENVPN_TLS_AUTH": _TLS_AUTH if self.tls_auth else "",
            "$OPENVPN_ADDITIONAL_CLIENT_CONFIG": self.additional_client_config,
            "$OPENVPN_ADDITIONAL_SERVER_CONFIG": self.additional_server_config,
        }
        if not self.tls_auth:
            variables["$OPENVPN_TA_KEY"] = ""
        return variables


def _additional_server_config(text: str) -> str:
    """The lines configure_container.sh puts after tls-version-min and the tls-auth line."""
    lines = text.replace("\r", "").split("\n")
    starts = [n for n, line in enumerate(lines) if line.startswith(_LAST_FIXED_LINE)]
    if not starts:
        return ""
    extra = [line for line in lines[starts[0] + 1 :] if line.strip() != _TLS_AUTH]
    return "\n".join(extra).strip()


def _subnet(server: str) -> IPv4Network:
    """`server 10.8.0.0 255.255.255.0` -> 10.8.0.0/24."""
    parts = server.split()
    if not parts:
        return DEFAULT_SUBNET
    mask = parts[1] if len(parts) > 1 else str(DEFAULT_SUBNET.netmask)
    try:
        return IPv4Network(f"{parts[0]}/{mask}")
    except (AddressValueError, NetmaskValueError) as error:
        raise ValueError(f"bad server line {server!r}") from error


@dataclass(frozen=True)
class IndexEntry:
    """One line of the easy-rsa pki/index.txt (the `openssl ca` database)."""

    status: str
    expires: str
    revoked: str
    serial: str
    file: str
    subject: str

    @property
    def common_name(self) -> str:
        match = re.search(r"/CN=([^/]*)", self.subject)
        return match[1] if match else ""

    @property
    def serial_number(self) -> int:
        return int(self.serial, 16)

    def line(self) -> str:
        return "\t".join(
            (self.status, self.expires, self.revoked, self.serial, self.file, self.subject)
        )


class CertificateIndex:
    """pki/index.txt: which certificates the CA issued and which it revoked.

    `hold` and `release` turn a certificate off and on again without deleting it; the CRL is
    made from this file by `easyrsa gen-crl`.
    """

    _VALID = "V"
    _REVOKED = "R"
    _FIELDS = 6

    def __init__(self, entries: list[IndexEntry]) -> None:
        self._entries = entries

    @classmethod
    def parse(cls, text: str) -> "CertificateIndex":
        entries = []
        for line in text.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != cls._FIELDS:
                raise ConfigFormatError("pki/index.txt has a line that is not six fields.")
            entries.append(IndexEntry(*fields))
        return cls(entries)

    def serialize(self) -> str:
        return "".join(f"{entry.line()}\n" for entry in self._entries)

    def valid_clients(self) -> list[str]:
        """Common names of the client certificates in force, in issue order."""
        return [
            entry.common_name
            for entry in self._entries
            if entry.status == self._VALID and entry.common_name != SERVER_CERT_NAME
        ]

    def is_held(self, serial: int) -> bool:
        return self._find(serial).status == self._REVOKED

    def hold(self, serial: int, when: datetime) -> None:
        """Revoke the certificate; it stays issued, so `release` can bring it back."""
        entry = self._find(serial)
        if entry.status != self._REVOKED:
            self._put(
                replace(entry, status=self._REVOKED, revoked=when.strftime(_REVOKED_AT_FORMAT))
            )

    def release(self, serial: int) -> None:
        entry = self._find(serial)
        if entry.status == self._REVOKED:
            self._put(replace(entry, status=self._VALID, revoked=""))

    def _find(self, serial: int) -> IndexEntry:
        for entry in self._entries:
            if entry.serial_number == serial:
                return entry
        raise ConfigFormatError(f"Certificate {serial:X} is not in pki/index.txt.")

    def _put(self, new: IndexEntry) -> None:
        self._entries = [
            new if entry.serial_number == new.serial_number else entry for entry in self._entries
        ]


@dataclass(frozen=True, repr=False)
class OpenVpnClientProfile:
    """Everything needed to hand an OpenVPN client to a user: .ovpn and vpn://.

    `private_key` is the user's credential; `repr` hides it.
    """

    host: str
    server: OpenVpnSettings
    client_id: str
    private_key: str
    ca_cert: str
    client_cert: str
    ta_key: str
    block_outside_dns: bool = False

    def __repr__(self) -> str:
        return f"OpenVpnClientProfile(host={self.host!r}, client_id={self.client_id!r})"

    def native_config(self) -> str:
        """OpenVpnConfigurator::createConfig; the DNS placeholders are left for the app."""
        variables = {**self.server.script_variables(), "$REMOTE_HOST": self.host}
        config = substitute(_CLIENT_TEMPLATE, variables)
        config = config.replace("$OPENVPN_CA_CERT", self.ca_cert)
        config = config.replace("$OPENVPN_CLIENT_CERT", self.client_cert)
        config = config.replace("$OPENVPN_PRIV_KEY", self.private_key)
        if "$OPENVPN_TA_KEY" in config:
            config = config.replace("$OPENVPN_TA_KEY", _sanitized_static_key(self.ta_key))
        else:
            config = config.replace("<tls-auth>", "").replace("</tls-auth>", "")
        # Every build of the app but Windows drops it, and the app sets it itself there.
        return config.replace("block-outside-dns", "")

    def vpn_config(
        self, description: str = "", dns: tuple[str, str] = DEFAULT_DNS
    ) -> dict[str, Any]:
        """Server JSON for vpn://; `last_config` is the client JSON as compact text."""
        client: dict[str, Any] = {
            "config": self.native_config(),
            "clientId": self.client_id,
            "block_outside_dns": self.block_outside_dns,
        }
        container = {
            "container": OPENVPN_CONTAINER,
            OPENVPN_PROTOCOL: {**self.server.to_json(), "last_config": to_qt_compact_json(client)},
        }
        return connection_config(self.host, container, description, dns)

    def ovpn_file(self, dns: tuple[str, str] = DEFAULT_DNS) -> str:
        """The .ovpn the app exports: processConfigWithExportSettings, generateOpenVpnConfig."""
        primary, secondary = dns
        config = self.native_config()
        config = config.replace("$PRIMARY_DNS", primary).replace("$SECONDARY_DNS", secondary)
        config = re.sub(r"redirect-gateway.*", "", config)
        if AMNEZIA_DNS_IP in primary:
            config = re.sub(f"dhcp-option DNS {re.escape(secondary)}", "", config)
        config += "\nredirect-gateway def1 ipv6 bypass-dhcp\n"
        config += "block-ipv6\n"
        config = config.replace("block-outside-dns", "")
        return "".join(f"{line}\n" for line in config.replace("\r", "").split("\n"))


def _sanitized_static_key(key: str) -> str:
    """Comment lines of ta.key are dropped; the key ends with a newline."""
    lines = [line for line in key.split("\n") if not line.strip().startswith("#")]
    result = "\n".join(lines)
    return result if result.endswith("\n") else result + "\n"


def is_ipv4(host: str) -> bool:
    try:
        IPv4Address(host)
    except AddressValueError:
        return False
    return True
