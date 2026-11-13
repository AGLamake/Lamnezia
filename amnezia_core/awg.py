"""Server config of the AmneziaWG 2.0 container: /opt/amnezia/awg/awg0.conf."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface, IPv4Network, ip_interface

from amnezia_core.containers import AWG2, WgContainer
from amnezia_core.errors import ConfigFormatError
from amnezia_core.wg_config import Section, WgConfig

# Files of the AmneziaWG 2.0 container.
AWG_CONFIG_PATH = AWG2.config_path
AWG_CLIENTS_TABLE_PATH = AWG2.clients_table_path
AWG_PSK_PATH = AWG2.psk_path

_SINGLE_HOST_PREFIX = 32

SPECIAL_JUNK = ("I1", "I2", "I3", "I4", "I5")

# The server does not use I1-I5: upstream keeps them as `# I1 = ...` comments and the app
# reads client values from there (AwgInstaller::extractConfigFromContainer).
_COMMENTED_SPECIAL_JUNK = re.compile(r"^\s*#\s*(?P<key>I[1-5])\s*=\s*(?P<value>.*?)\s*$")

# Obfuscation parameters of [Interface] in the order upstream configure_container.sh writes them.
AWG_INTERFACE_PARAMS = (
    "Jc",
    "Jmin",
    "Jmax",
    "S1",
    "S2",
    "S3",
    "S4",
    "H1",
    "H2",
    "H3",
    "H4",
    "HeaderProtectionKey",
    "ContentPaddingAddition",
    "RekeyAfterTime",
    "RekeyTimeout",
    "RejectAfterTime",
    "KeepaliveTimeout",
    "MaxHandshakeAttempts",
    "RandomTrailers",
    "DisableCookies",
    *SPECIAL_JUNK,
)
# What the app lets one change in legacy AmneziaWG (awg_legacy/configure_container.sh).
LEGACY_AWG_PARAMS = ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4", *SPECIAL_JUNK)


def interface_params(kind: WgContainer) -> tuple[str, ...]:
    """The obfuscation parameters a container has; WireGuard has none."""
    if kind == AWG2:
        return AWG_INTERFACE_PARAMS
    return LEGACY_AWG_PARAMS if kind.obfuscated else ()


@dataclass(frozen=True)
class Peer:
    """Client entry of the server config; `public_key` is the clientId in clientsTable."""

    public_key: str
    allowed_ips: tuple[str, ...]
    preshared_key: str | None = None

    @classmethod
    def from_section(cls, section: Section) -> "Peer":
        public_key = section.get("PublicKey")
        if not public_key:
            raise ConfigFormatError("A [Peer] section has no PublicKey.")
        allowed_ips = tuple(
            ip.strip() for value in section.get_all("AllowedIPs") for ip in value.split(",")
        )
        return cls(public_key, tuple(ip for ip in allowed_ips if ip), section.get("PresharedKey"))

    def to_section(self) -> Section:
        """Section text identical to what the Amnezia app appends when it adds a client."""
        entries = [("PublicKey", self.public_key)]
        if self.preshared_key:
            entries.append(("PresharedKey", self.preshared_key))
        entries.append(("AllowedIPs", ", ".join(self.allowed_ips)))
        return Section.build("Peer", entries)

    @property
    def addresses(self) -> list[IPv4Address]:
        """Single-host IPv4 addresses from AllowedIPs: the client tunnel IPs."""
        hosts = (ip_interface(ip) for ip in self.allowed_ips)
        return [
            host.ip
            for host in hosts
            if isinstance(host, IPv4Interface) and host.network.prefixlen == _SINGLE_HOST_PREFIX
        ]


@dataclass(frozen=True)
class Renumbering:
    """The move of the client subnet: every address keeps its place in the network.

    10.8.1.5 of 10.8.1.0/24 becomes 10.9.0.5 of 10.9.0.0/24, so the server and each client get
    the same host part and no two clients meet on one address.
    """

    old: IPv4Network
    new: IPv4Network

    def __post_init__(self) -> None:
        if self.new.num_addresses < self.old.num_addresses:
            raise ConfigFormatError(f"{self.new} is smaller than {self.old}.")

    def address(self, ip: IPv4Address) -> IPv4Address:
        """The new address of `ip`; an address outside the old subnet stays as it is."""
        if ip not in self.old:
            return ip
        return self.new.network_address + (int(ip) - int(self.old.network_address))

    def interface(self, interface: IPv4Interface) -> IPv4Interface:
        return IPv4Interface((self.address(interface.ip), self.new.prefixlen))


class AwgServerConfig:
    """awg0.conf with typed access to the interface and peers. Unknown lines are kept as is."""

    def __init__(self, document: WgConfig) -> None:
        interfaces = document.sections_named("Interface")
        if len(interfaces) != 1:
            raise ConfigFormatError("awg0.conf must have exactly one [Interface] section.")
        self._document = document
        self.interface = interfaces[0]

    @classmethod
    def parse(cls, text: str) -> "AwgServerConfig":
        return cls(WgConfig.parse(text))

    def serialize(self) -> str:
        return self._document.serialize()

    @property
    def private_key(self) -> str | None:
        return self.interface.get("PrivateKey")

    @property
    def listen_port(self) -> int | None:
        port = self.interface.get("ListenPort")
        return int(port) if port else None

    @property
    def address(self) -> IPv4Interface:
        """Server tunnel address with the client subnet, e.g. 10.8.1.0/24."""
        for value in (self.interface.get("Address") or "").split(","):
            try:
                address = IPv4Interface(value.strip())
            except ValueError:
                continue
            return address
        raise ConfigFormatError("awg0.conf has no IPv4 Address in [Interface].")

    @property
    def awg_params(self) -> dict[str, str]:
        """AWG obfuscation parameters present in the file, I1-I5 also from comments."""
        params = {**self._commented_special_junk(), **dict(self.interface.entries())}
        return {name: params[name] for name in AWG_INTERFACE_PARAMS if params.get(name)}

    def update_interface(self, port: int, params: Mapping[str, str]) -> None:
        """Set ListenPort and all AWG parameters; an empty one loses its line, as upstream writes.

        I1-I5 go into `# I1 = ...` comments at the end of [Interface], where the app reads them.
        """
        self.interface.set("ListenPort", str(port))
        for name in AWG_INTERFACE_PARAMS:
            value = params.get(name, "").strip()
            if name in SPECIAL_JUNK:
                self.interface.remove(name)
                self._set_special_junk(name, value)
            elif value:
                self.interface.set(name, value)
            else:
                self.interface.remove(name)

    def renumber(self, renumbering: Renumbering) -> None:
        """Address of [Interface] and the client addresses of AllowedIPs in the new subnet."""
        address = self.address
        values = (self.interface.get("Address") or "").split(",")
        self.interface.set(
            "Address",
            ", ".join(
                str(renumbering.interface(address)) if _is(value, address) else value.strip()
                for value in values
            ),
        )
        for section in self._peer_sections():
            peer = Peer.from_section(section)
            ips = ", ".join(_renumbered(ip, renumbering) for ip in peer.allowed_ips)
            if len(section.get_all("AllowedIPs")) > 1:
                section.remove("AllowedIPs")
            section.set("AllowedIPs", ips)

    @property
    def peers(self) -> list[Peer]:
        return [Peer.from_section(section) for section in self._peer_sections()]

    def find_peer(self, public_key: str) -> Peer | None:
        return next((peer for peer in self.peers if peer.public_key == public_key), None)

    def add_peer(self, peer: Peer) -> None:
        """Append the peer at the end of the file, as the Amnezia app does."""
        if self.find_peer(peer.public_key) is not None:
            raise ConfigFormatError(f"Peer {peer.public_key} is already in awg0.conf.")
        self._document.append(peer.to_section())

    def remove_peer(self, public_key: str) -> bool:
        """Drop the peer section with its trailing lines; False if there was no such peer."""
        for section in self._peer_sections():
            if section.get("PublicKey") == public_key:
                self._document.remove(section)
                return True
        return False

    def _set_special_junk(self, name: str, value: str) -> None:
        lines = self.interface.lines
        lines[:] = [line for line in lines if _special_junk_key(line) != name]
        if not value:
            return
        if "\n" in value or "\r" in value:
            raise ConfigFormatError(f"Unsafe value for {name}.")
        last = max(i for i, line in enumerate(lines) if line.strip())
        lines.insert(last + 1, f"# {name} = {value}")

    def _commented_special_junk(self) -> dict[str, str]:
        matches = (_COMMENTED_SPECIAL_JUNK.match(line) for line in self.interface.lines)
        return {match["key"]: match["value"] for match in matches if match}

    def _peer_sections(self) -> list[Section]:
        return self._document.sections_named("Peer")


def _is(value: str, address: IPv4Interface) -> bool:
    try:
        return IPv4Interface(value.strip()) == address
    except ValueError:
        return False


def _renumbered(allowed_ip: str, renumbering: Renumbering) -> str:
    """A client address of AllowedIPs moved to the new subnet; other routes stay."""
    host = ip_interface(allowed_ip)
    if not isinstance(host, IPv4Interface) or host.network.prefixlen != _SINGLE_HOST_PREFIX:
        return allowed_ip
    return f"{renumbering.address(host.ip)}/{_SINGLE_HOST_PREFIX}"


def _special_junk_key(line: str) -> str | None:
    match = _COMMENTED_SPECIAL_JUNK.match(line)
    return match["key"] if match else None
