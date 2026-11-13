"""Client profile of a WireGuard-family container exactly as the Amnezia app exports it.

AmneziaWG 2.0, legacy AmneziaWG and WireGuard share the code; they differ in the client
template, the container name and the server JSON. Mirrors upstream
WireguardConfigurator/AwgConfigurator::createConfig, AwgServerConfig::toJson,
WireGuardServerConfig::toJson, AwgClientConfig::toJson and
ExportController::generateConnectionConfig.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Interface
from typing import Any

from amnezia_core.awg import AWG_INTERFACE_PARAMS, SPECIAL_JUNK, AwgServerConfig
from amnezia_core.containers import AWG2, AWG_LEGACY, WIREGUARD, WgContainer
from amnezia_core.errors import ConfigFormatError
from amnezia_core.keys import KeyPair
from amnezia_core.qt_json import to_qt_compact_json
from amnezia_core.script_vars import PARAM_VARIABLES, TOGGLES, is_toggle_enabled, substitute
from amnezia_core.vpn_link import DEFAULT_DNS, connection_config

AWG_CONTAINER = AWG2.name
DEFAULT_MTU = "1376"

_TRANSPORT_PROTO = "udp"
_ALLOWED_IPS = ["0.0.0.0/0", "::/0"]
_KEEPALIVE = "25"
_AWG3_KEEPALIVE = "25-35"
_AWG3_VALUES = (
    "HeaderProtectionKey",
    "ContentPaddingAddition",
    "RekeyAfterTime",
    "RekeyTimeout",
    "RejectAfterTime",
    "KeepaliveTimeout",
    "MaxHandshakeAttempts",
)
# The parameters AmneziaWG 3.1 added to awg0.conf.
AWG3_PARAMS = (*_AWG3_VALUES, *TOGGLES)
AWG3_VERSION = "3.1"

# Verbatim copies of amnezia-client client/server_scripts/<type>/template.conf.
# Copyright (C) Amnezia VPN contributors, GPL-3.0 (see NOTICE).
_AWG2_TEMPLATE = """\
[Interface]
Address = $WIREGUARD_CLIENT_IP/32
DNS = $PRIMARY_DNS, $SECONDARY_DNS
PrivateKey = $WIREGUARD_CLIENT_PRIVATE_KEY
Jc = $JUNK_PACKET_COUNT
Jmin = $JUNK_PACKET_MIN_SIZE
Jmax = $JUNK_PACKET_MAX_SIZE
S1 = $INIT_PACKET_JUNK_SIZE
S2 = $RESPONSE_PACKET_JUNK_SIZE
S3 = $COOKIE_REPLY_PACKET_JUNK_SIZE
S4 = $TRANSPORT_PACKET_JUNK_SIZE
H1 = $INIT_PACKET_MAGIC_HEADER
H2 = $RESPONSE_PACKET_MAGIC_HEADER
H3 = $UNDERLOAD_PACKET_MAGIC_HEADER
H4 = $TRANSPORT_PACKET_MAGIC_HEADER
I1 = $SPECIAL_JUNK_1
I2 = $SPECIAL_JUNK_2
I3 = $SPECIAL_JUNK_3
I4 = $SPECIAL_JUNK_4
I5 = $SPECIAL_JUNK_5
HeaderProtectionKey = $HEADER_PROTECTION_KEY
ContentPaddingAddition = $CONTENT_PADDING_ADDITION
RekeyAfterTime = $REKEY_AFTER_TIME
RekeyTimeout = $REKEY_TIMEOUT
RejectAfterTime = $REJECT_AFTER_TIME
KeepaliveTimeout = $KEEPALIVE_TIMEOUT
MaxHandshakeAttempts = $MAX_HANDSHAKE_ATTEMPTS
RandomTrailers = $RANDOM_TRAILERS
DisableCookies = $DISABLE_COOKIES

[Peer]
PublicKey = $WIREGUARD_SERVER_PUBLIC_KEY
PresharedKey = $WIREGUARD_PSK
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = $SERVER_IP_ADDRESS:$AWG_SERVER_PORT
PersistentKeepalive = $PERSISTENT_KEEPALIVE
"""

_AWG_LEGACY_TEMPLATE = """\
[Interface]
Address = $WIREGUARD_CLIENT_IP/32
DNS = $PRIMARY_DNS, $SECONDARY_DNS
PrivateKey = $WIREGUARD_CLIENT_PRIVATE_KEY
Jc = $JUNK_PACKET_COUNT
Jmin = $JUNK_PACKET_MIN_SIZE
Jmax = $JUNK_PACKET_MAX_SIZE
S1 = $INIT_PACKET_JUNK_SIZE
S2 = $RESPONSE_PACKET_JUNK_SIZE
H1 = $INIT_PACKET_MAGIC_HEADER
H2 = $RESPONSE_PACKET_MAGIC_HEADER
H3 = $UNDERLOAD_PACKET_MAGIC_HEADER
H4 = $TRANSPORT_PACKET_MAGIC_HEADER
I1 = $SPECIAL_JUNK_1
I2 = $SPECIAL_JUNK_2
I3 = $SPECIAL_JUNK_3
I4 = $SPECIAL_JUNK_4
I5 = $SPECIAL_JUNK_5

[Peer]
PublicKey = $WIREGUARD_SERVER_PUBLIC_KEY
PresharedKey = $WIREGUARD_PSK
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = $SERVER_IP_ADDRESS:$AWG_SERVER_PORT
PersistentKeepalive = 25
"""

_WIREGUARD_TEMPLATE = """\
[Interface]
Address = $WIREGUARD_CLIENT_IP/32
DNS = $PRIMARY_DNS, $SECONDARY_DNS
PrivateKey = $WIREGUARD_CLIENT_PRIVATE_KEY

[Peer]
PublicKey = $WIREGUARD_SERVER_PUBLIC_KEY
PresharedKey = $WIREGUARD_PSK
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = $SERVER_IP_ADDRESS:$WIREGUARD_SERVER_PORT
PersistentKeepalive = 25
"""

_CLIENT_TEMPLATES = {
    AWG2: _AWG2_TEMPLATE,
    AWG_LEGACY: _AWG_LEGACY_TEMPLATE,
    WIREGUARD: _WIREGUARD_TEMPLATE,
}

_EMPTY_VALUE_LINE = re.compile(r"^\s*\S+\s*=\s*$")


def has_awg3_params(params: Mapping[str, str]) -> bool:
    """The parameters are of AmneziaWG 3.1, as upstream awgVersionOf decides."""
    return any(params.get(name, "").strip() for name in _AWG3_VALUES) or any(
        is_toggle_enabled(params.get(name, "")) for name in TOGGLES
    )


def protocol_version(params: Mapping[str, str]) -> str:
    """Same detection as upstream awgVersionOf: "3.1", "2", "1.5" or empty."""
    if has_awg3_params(params):
        return AWG3_VERSION
    headers = (params.get(f"H{i}", "") for i in "1234")
    if params.get("S3") or params.get("S4") or any("-" in header for header in headers):
        return "2"
    return "1.5" if any(params.get(name) for name in SPECIAL_JUNK) else ""


def awg_version(kind: WgContainer, params: Mapping[str, str]) -> str:
    """The AmneziaWG version to show for a container: "3.1", "2", "1.5" or "1.0"; empty for WG.

    An AmneziaWG 2.0 container whose parameters give no version is still 2.
    """
    if not kind.obfuscated:
        return ""
    return protocol_version(params) or ("2" if kind == AWG2 else "1.0")


@dataclass(frozen=True)
class AwgServerSettings:
    """Server half of a profile: what upstream AwgServerConfig holds after reading awg0.conf."""

    port: int
    subnet: IPv4Interface
    params: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: AwgServerConfig, port: int | None = None) -> "AwgServerSettings":
        """`port` is the published container port; by default ListenPort from the file."""
        port = port or config.listen_port
        if port is None:
            raise ConfigFormatError("awg0.conf has no ListenPort and no port was given.")
        return cls(port, config.address, config.awg_params)

    def param(self, name: str) -> str:
        return self.params.get(name, "")

    @property
    def has_awg3_params(self) -> bool:
        return has_awg3_params(self.params)

    @property
    def protocol_version(self) -> str:
        return protocol_version(self.params)

    @property
    def persistent_keepalive(self) -> str:
        return _AWG3_KEEPALIVE if self.has_awg3_params else _KEEPALIVE

    def to_json(self) -> dict[str, Any]:
        """AwgServerConfig::toJson: empty values are omitted, except I1-I5."""
        data = {
            "port": str(self.port),
            "transport_proto": _TRANSPORT_PROTO,
            "protocol_version": self.protocol_version,
            "subnet_address": str(self.subnet.ip),
            "subnet_cidr": str(self.subnet.network.prefixlen),
            **{name: self.param(name) for name in AWG_INTERFACE_PARAMS},
        }
        return {key: value for key, value in data.items() if value or key in SPECIAL_JUNK}

    def to_wireguard_json(self) -> dict[str, str]:
        """WireGuardServerConfig::toJson with the fields InstallerBase::createBaseConfig sets."""
        return {
            "port": str(self.port),
            "transport_proto": _TRANSPORT_PROTO,
            "subnet_address": str(self.subnet.ip),
        }


@dataclass(frozen=True, repr=False)
class AwgClientProfile:
    """Everything needed to hand a peer to a client: native .conf, vpn:// JSON."""

    host: str
    server: AwgServerSettings
    server_public_key: str
    preshared_key: str
    keys: KeyPair
    client_ip: IPv4Address
    mtu: str = DEFAULT_MTU
    container: WgContainer = AWG2

    def __repr__(self) -> str:
        return f"AwgClientProfile(host={self.host!r}, client_ip={self.client_ip!r})"

    def native_config(self) -> str:
        """Config text stored in `last_config`; DNS stays as placeholders the app fills in."""
        variables = {
            "$SERVER_IP_ADDRESS": self.host,
            "$AWG_SERVER_PORT": str(self.server.port),
            "$WIREGUARD_SERVER_PORT": str(self.server.port),
            "$PERSISTENT_KEEPALIVE": self.server.persistent_keepalive,
            **{variable: self._param_value(name) for variable, name in PARAM_VARIABLES.items()},
        }
        text = substitute(_CLIENT_TEMPLATES[self.container], variables)
        text = "\n".join(line for line in text.split("\n") if not _EMPTY_VALUE_LINE.match(line))
        client_values = {
            "$WIREGUARD_CLIENT_PRIVATE_KEY": self.keys.private_key,
            "$WIREGUARD_CLIENT_IP": str(self.client_ip),
            "$WIREGUARD_SERVER_PUBLIC_KEY": self.server_public_key,
            "$WIREGUARD_PSK": self.preshared_key,
        }
        return substitute(text, client_values)

    def conf_file(self, dns: tuple[str, str] = DEFAULT_DNS) -> str:
        """Native .conf for the AmneziaWG or WireGuard app, as ExportController builds it."""
        text = self.native_config().replace("$PRIMARY_DNS", dns[0])
        # Upstream re-joins lines with a newline after each, which doubles the final one.
        return text.replace("$SECONDARY_DNS", dns[1]) + "\n"

    def client_json(self) -> dict[str, Any]:
        """AwgClientConfig::toJson."""
        native = self.native_config()
        return {
            "config": native,
            "hostName": self.host,
            "port": self.server.port,
            "client_ip": str(self.client_ip),
            "client_priv_key": self.keys.private_key,
            "client_pub_key": self.keys.public_key,
            "server_pub_key": self.server_public_key,
            "psk_key": self.preshared_key,
            "clientId": self.keys.public_key,
            "allowed_ips": list(_ALLOWED_IPS),
            "persistent_keep_alive": self.server.persistent_keepalive,
            "mtu": self.mtu,
            **_awg_values_of(native),
        }

    def vpn_config(
        self, description: str = "", dns: tuple[str, str] = DEFAULT_DNS
    ) -> dict[str, Any]:
        """Server JSON that goes into vpn:// (no SSH credentials, one container)."""
        server: dict[str, Any] = (
            self.server.to_json() if self.container.obfuscated else self.server.to_wireguard_json()
        )
        container = {
            "container": self.container.name,
            self.container.protocol: {
                **server,
                "last_config": to_qt_compact_json(self.client_json()),
            },
        }
        return connection_config(self.host, container, description, dns)

    def _param_value(self, name: str) -> str:
        value = self.server.param(name)
        if name in TOGGLES and not is_toggle_enabled(value):
            return ""
        return value


def _awg_values_of(native: str) -> dict[str, str]:
    """AWG parameters as upstream reads them back from the rendered config (`key = value`)."""
    values = {}
    for line in native.split("\n"):
        key, *rest = line.strip().split(" = ")
        if len(rest) == 1 and key in AWG_INTERFACE_PARAMS and rest[0].strip():
            values[key] = rest[0].strip()
    return values
