"""Config of the fallback relay and the state file its agent writes (docs/FALLBACK.md section 5).

The panel writes the config, the agent on the relay reads it and works on its own: it probes the
members of each group with a new handshake of the service peer and moves the group port to the
next backend by priority after `failures` failed probes in a row. The agent writes what it sees
into the state file, which the panel reads over SSH. For planned maintenance the panel may
also write a switch request; the agent answers it in the state file. The files are JSON with a
`version`, so an agent and a panel of different releases notice a format they do not know.

A group in split routing (docs/SPLIT.md) ends the tunnel on the relay: a clone of the group
answers its clients and sends part of the traffic straight out, the rest over the uplink, a
client of the group on the active member. Its list mode says which part the list names: the
networks sent straight out (blacklist, Russian networks) or the only ones sent abroad
(whitelist). Its peers, its list of networks and its own domains,
which the agent resolves, come in files of their own, so issuing a key or a new list does not
rewrite the config. The state tells the status of the relay's resolver for the split groups'
clients, never its queries.
"""

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from ipaddress import IPv4Address, IPv4Interface, IPv4Network, collapse_addresses
from typing import Any

from amnezia_core.awg import AWG_INTERFACE_PARAMS, SPECIAL_JUNK, Peer
from amnezia_core.errors import AmneziaFormatError, RelayFormatError
from amnezia_core.keys import decode_key
from amnezia_core.split_lists import ListMode

FORMAT_VERSION = 1
# Measured in P12-01 (docs/research/relay-fallback.md): a failure is seen in about 14 s, which
# does not lengthen the 18-22 s outage the client has anyway.
DEFAULT_CHECK_INTERVAL = 5
DEFAULT_FAILURES = 3
# Good uplink checks in a row that take a split group back from the emergency forward.
DEFAULT_RECOVERY = 6
# The agent checks the uplink by a ping through it: an answer means traffic leaves the member.
DEFAULT_UPLINK_CHECK = IPv4Address("1.1.1.1")

_MAX_PORT = 65535


@dataclass(frozen=True)
class Backend:
    """A group member as the relay forwards to it: an IPv4 address for nftables DNAT and a port."""

    host: IPv4Address
    port: int

    def __post_init__(self) -> None:
        _check_port(self.port)

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        return {"host": str(self.host), "port": self.port}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Backend":
        try:
            host = IPv4Address(_string(data, "host"))
        except ValueError as error:
            raise RelayFormatError(f"Backend host is not an IPv4 address: {error}") from error
        return cls(host, _integer(data, "port"))


@dataclass(frozen=True)
class ServicePeer:
    """Group client the agent probes members with; it is never given to a user.

    It carries everything a client config of the group has, so a handshake with any member
    succeeds: own keys and tunnel address, the server key, the preshared key and the
    obfuscation parameters of [Interface].
    """

    private_key: str = field(repr=False)
    address: IPv4Interface
    server_public_key: str
    preshared_key: str = field(repr=False)
    awg_params: Mapping[str, str]

    def __post_init__(self) -> None:
        for key in (self.private_key, self.server_public_key, self.preshared_key):
            decode_key(key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "private_key": self.private_key,
            "address": str(self.address),
            "server_public_key": self.server_public_key,
            "preshared_key": self.preshared_key,
            "awg_params": dict(self.awg_params),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ServicePeer":
        return cls(
            private_key=_string(data, "private_key"),
            address=_interface(data, "address"),
            server_public_key=_string(data, "server_public_key"),
            preshared_key=_string(data, "preshared_key"),
            awg_params=_params(data),
        )


class Routing(StrEnum):
    """How the relay carries a group: forwards its port to a member or splits the traffic itself."""

    FORWARD = "forward"
    SPLIT = "split"


class UplinkKind(StrEnum):
    """What carries the uplink of a split group to the member; XRay Reality may join AmneziaWG."""

    AWG = "awg"


@dataclass(frozen=True)
class Clone:
    """The group's server on the relay: its key, its address in the group subnet and the
    obfuscation parameters of the group's awg0.conf. It listens on the group port.

    The address is the one awg0.conf of the members has: Amnezia gives the server the network
    address of the subnet (`10.8.1.0/24`) and clients the hosts from `.1`.
    """

    private_key: str = field(repr=False)
    address: IPv4Interface
    awg_params: Mapping[str, str]

    def __post_init__(self) -> None:
        decode_key(self.private_key)
        if self.address.ip == self.address.network.broadcast_address:
            raise RelayFormatError(f"Clone address {self.address} is the broadcast address.")

    @classmethod
    def of_server(cls, private_key: str, subnet: str, params: Mapping[str, str]) -> "Clone":
        """The clone of a group server: its key, its `Address` and the parameters of its
        [Interface]. The server does not use I1-I5, upstream keeps them as comments."""
        server_params = {
            name: value
            for name in AWG_INTERFACE_PARAMS
            if name not in SPECIAL_JUNK and (value := str(params.get(name, "")))
        }
        return cls(private_key, IPv4Interface(subnet), server_params)

    def to_dict(self) -> dict[str, Any]:
        return {
            "private_key": self.private_key,
            "address": str(self.address),
            "awg_params": dict(self.awg_params),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Clone":
        return cls(_string(data, "private_key"), _interface(data, "address"), _params(data))


@dataclass(frozen=True)
class SplitRouting:
    """What a split group needs on the relay: its clone and the uplink peer, a client of the
    group that the relay connects to the active member with.

    `dns` are the DNS addresses of the group's keys: the relay answers the clients' queries to
    them with its own resolver, so Russian names resolve to Russian addresses. With `dns_all`
    it answers every plain DNS query of the clients, to a DNS server set on a device too.
    `list_mode` says what the network list of the group holds.
    """

    clone: Clone
    uplink: ServicePeer
    kind: UplinkKind = UplinkKind.AWG
    dns: tuple[IPv4Address, ...] = ()
    list_mode: ListMode = ListMode.EXCLUDE
    dns_all: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "clone": self.clone.to_dict(),
            "uplink": self.uplink.to_dict(),
            "dns": [str(address) for address in self.dns],
            "list_mode": str(self.list_mode),
            "dns_all": self.dns_all,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SplitRouting":
        dns_all = data.get("dns_all", False)
        if not isinstance(dns_all, bool):
            raise RelayFormatError("Split routing needs a boolean dns_all.")
        return cls(
            clone=Clone.from_dict(_object(data, "clone")),
            uplink=ServicePeer.from_dict(_object(data, "uplink")),
            kind=_choice(UplinkKind, data.get("kind", UplinkKind.AWG), "uplink kind"),
            dns=_addresses(data, "dns"),
            list_mode=_choice(ListMode, data.get("list_mode", ListMode.EXCLUDE), "list mode"),
            dns_all=dns_all,
        )


@dataclass(frozen=True)
class RelayGroup:
    """A group behind the relay: its public UDP port, the members in priority order and how
    the relay carries it; a split group also has its clone and uplink."""

    port: int
    backends: tuple[Backend, ...]
    service: ServicePeer
    routing: Routing = Routing.FORWARD
    split: SplitRouting | None = None

    def __post_init__(self) -> None:
        _check_port(self.port)
        if not self.backends:
            raise RelayFormatError(f"Group on port {self.port} has no backends.")
        if len(set(self.backends)) != len(self.backends):
            raise RelayFormatError(f"Group on port {self.port} lists a backend twice.")
        if self.routing is Routing.SPLIT and self.split is None:
            raise RelayFormatError(f"Split group on port {self.port} has no clone and uplink.")

    def to_dict(self) -> dict[str, Any]:
        data = {
            "port": self.port,
            "backends": [backend.to_dict() for backend in self.backends],
            "service": self.service.to_dict(),
            "routing": str(self.routing),
        }
        if self.split is not None:
            data["split"] = self.split.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RelayGroup":
        split = data.get("split")
        if split is not None and not isinstance(split, dict):
            raise RelayFormatError("'split' must be an object.")
        return cls(
            port=_integer(data, "port"),
            backends=tuple(Backend.from_dict(item) for item in _objects(data, "backends")),
            service=ServicePeer.from_dict(_object(data, "service")),
            routing=_choice(Routing, data.get("routing", Routing.FORWARD), "routing"),
            split=SplitRouting.from_dict(split) if split is not None else None,
        )


@dataclass(frozen=True)
class RelayConfig:
    """Everything the agent needs: groups, how often to probe and after how many failures to switch.

    `recovery` good uplink checks in a row take a split group back from the emergency forward;
    `uplink_check` is the address the agent pings through the uplink. The file holds private
    keys: the panel writes it readable by root only.
    """

    groups: tuple[RelayGroup, ...]
    check_interval: int = DEFAULT_CHECK_INTERVAL
    failures: int = DEFAULT_FAILURES
    recovery: int = DEFAULT_RECOVERY
    uplink_check: IPv4Address = DEFAULT_UPLINK_CHECK

    def __post_init__(self) -> None:
        if min(self.check_interval, self.failures, self.recovery) < 1:
            raise RelayFormatError("check_interval, failures and recovery must be at least 1.")
        ports = [group.port for group in self.groups]
        if len(set(ports)) != len(ports):
            raise RelayFormatError("Two groups of the relay share a port.")

    def to_json(self) -> str:
        data = {
            "version": FORMAT_VERSION,
            "check_interval": self.check_interval,
            "failures": self.failures,
            "recovery": self.recovery,
            "uplink_check": str(self.uplink_check),
            "groups": [group.to_dict() for group in self.groups],
        }
        return json.dumps(data, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "RelayConfig":
        data = _document(text, "relay config")
        return _parsed(
            lambda: cls(
                groups=tuple(RelayGroup.from_dict(item) for item in _objects(data, "groups")),
                check_interval=_integer(data, "check_interval"),
                failures=_integer(data, "failures"),
                recovery=_integer(data, "recovery") if "recovery" in data else DEFAULT_RECOVERY,
                uplink_check=_address(data, "uplink_check", DEFAULT_UPLINK_CHECK),
            )
        )


def networks_file(port: int) -> str:
    """Name of the list file of a split group in the agent folder."""
    return f"networks-{port}.txt"


def domains_file(port: int) -> str:
    """Name of the file with the own domains of a split group in the agent folder."""
    return f"domains-{port}.txt"


def clone_peers_file(port: int) -> str:
    """Name of the file with the `[Peer]` sections of a split group's clone in the agent folder."""
    return f"peers-{port}.conf"


def speed_file(port: int) -> str:
    """Name of the tc script with the speed limits of a split group's clone in the agent folder."""
    return f"speed-{port}.sh"


def clone_interface(port: int) -> str:
    """Name of the interface of a split group's clone in the relay container."""
    return f"lg{port}"


@dataclass(frozen=True)
class ClonePeers:
    """The `[Peer]` sections of a split group's clone: the key peers of the group, written as
    awg0.conf has them. The agent appends them to the clone's `[Interface]`."""

    peers: tuple[Peer, ...]

    def __post_init__(self) -> None:
        keys = [peer.public_key for peer in self.peers]
        if len(set(keys)) != len(keys):
            raise RelayFormatError("A clone lists a peer twice.")
        for key in keys:
            decode_key(key)

    def to_text(self) -> str:
        return "".join("\n".join(peer.to_section().lines) + "\n" for peer in self.peers)


@dataclass(frozen=True)
class DomainList:
    """Own domains of a split group: the agent resolves them and routes their addresses.

    One domain per line after its direction, `local` (straight out of the relay) or `abroad`
    (over the uplink, even when the address is in the network list). The panel writes no file
    for a group without domains; the agent then routes none.
    """

    local: tuple[str, ...] = ()
    abroad: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.local or self.abroad)

    def to_text(self) -> str:
        lines = [f"local {name}" for name in self.local]
        lines += [f"abroad {name}" for name in self.abroad]
        return "".join(f"{line}\n" for line in lines)


@dataclass(frozen=True)
class NetworkList:
    """The network list of a split group: in the blacklist mode the networks sent straight out
    of the relay, in the whitelist mode the only ones sent over the uplink.

    The file has one network per line, `#` starts a comment. It is never empty: the agent
    refuses an empty or broken file and keeps the list it has.
    """

    networks: tuple[IPv4Network, ...]

    def __post_init__(self) -> None:
        if not self.networks:
            raise RelayFormatError("A network list must not be empty.")

    @classmethod
    def of(cls, networks: Iterable[IPv4Network]) -> "NetworkList":
        """The list with overlapping and adjacent networks merged, in address order."""
        return cls(tuple(collapse_addresses(networks)))

    def to_text(self) -> str:
        return "".join(f"{network}\n" for network in self.networks)

    @classmethod
    def parse(cls, text: str) -> "NetworkList":
        networks = []
        for number, line in enumerate(text.splitlines(), start=1):
            entry = line.split("#", 1)[0].strip()
            if not entry:
                continue
            try:
                networks.append(IPv4Network(entry))
            except ValueError as error:
                raise RelayFormatError(f"Line {number} is not an IPv4 network: {error}") from error
        return cls.of(networks)


@dataclass(frozen=True)
class BackendState:
    """What the agent last saw of one member: reachable from the relay or not, and since when."""

    backend: Backend
    alive: bool
    failures: int
    checked_at: datetime | None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BackendState":
        alive = data.get("alive")
        if not isinstance(alive, bool):
            raise RelayFormatError("Backend state needs a boolean alive.")
        return cls(
            backend=Backend.from_dict(data),
            alive=alive,
            failures=_integer(data, "failures"),
            checked_at=_moment(data, "checked_at"),
        )


@dataclass(frozen=True)
class UplinkState:
    """The uplink of a split group: the member it points to and whether the last check passed."""

    endpoint: Backend | None
    alive: bool
    checked_at: datetime | None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UplinkState":
        endpoint = data.get("endpoint")
        if endpoint is not None and not isinstance(endpoint, dict):
            raise RelayFormatError("Uplink endpoint must be a backend or null.")
        alive = data.get("alive")
        if not isinstance(alive, bool):
            raise RelayFormatError("Uplink state needs a boolean alive.")
        return cls(
            endpoint=Backend.from_dict(endpoint) if endpoint is not None else None,
            alive=alive,
            checked_at=_moment(data, "checked_at"),
        )


@dataclass(frozen=True)
class GroupState:
    """State of one group: the active backend, the last switch and the forwarded bytes.

    `unanswered`: clients reach the active backend and none gets an answer. `routing` is what
    the config asks, `mode` what the agent does: a split group in `forward` mode is in the
    emergency forward (docs/SPLIT.md 4.3). `networks` counts the applied list of a split group.
    `bytes_direct_*` is the part of the bytes of a split group that went straight out of the
    relay, not through its uplink. An agent of an older release tells none of these.
    """

    port: int
    active: Backend | None
    switched_at: datetime | None
    backends: tuple[BackendState, ...]
    bytes_from_clients: int
    bytes_to_clients: int
    unanswered: bool = False
    routing: Routing = Routing.FORWARD
    mode: Routing = Routing.FORWARD
    mode_changed_at: datetime | None = None
    uplink: UplinkState | None = None
    networks: int = 0
    bytes_direct_from_clients: int | None = None
    bytes_direct_to_clients: int | None = None

    @property
    def emergency(self) -> bool:
        """A split group the agent forwards because its uplink fails and a member answers."""
        return self.routing is Routing.SPLIT and self.mode is Routing.FORWARD

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GroupState":
        active = data.get("active")
        if active is not None and not isinstance(active, dict):
            raise RelayFormatError("Group state active must be a backend or null.")
        uplink = data.get("uplink")
        if uplink is not None and not isinstance(uplink, dict):
            raise RelayFormatError("Group state uplink must be an object or null.")
        return cls(
            routing=_choice(Routing, data.get("routing", Routing.FORWARD), "routing"),
            mode=_choice(Routing, data.get("mode", Routing.FORWARD), "mode"),
            mode_changed_at=_moment(data, "mode_changed_at"),
            uplink=UplinkState.from_dict(uplink) if uplink is not None else None,
            networks=_integer(data, "networks") if "networks" in data else 0,
            unanswered=data.get("unanswered") is True,
            port=_integer(data, "port"),
            active=Backend.from_dict(active) if active is not None else None,
            switched_at=_moment(data, "switched_at"),
            backends=tuple(BackendState.from_dict(item) for item in _objects(data, "backends")),
            bytes_from_clients=_integer(data, "bytes_from_clients"),
            bytes_to_clients=_integer(data, "bytes_to_clients"),
            bytes_direct_from_clients=_optional_integer(data, "bytes_direct_from_clients"),
            bytes_direct_to_clients=_optional_integer(data, "bytes_direct_to_clients"),
        )


class ResolverStatus(StrEnum):
    """The relay's resolver: `off` while no group is split, `missing` in an image without
    unbound, `unknown` from an agent of an older release that does not tell."""

    UNKNOWN = "unknown"
    OFF = "off"
    RUNNING = "running"
    FAILED = "failed"
    MISSING = "missing"


@dataclass(frozen=True)
class ResolverState:
    """The status of the relay's resolver and, when it failed, why; `lookup_error` says why the
    agent's last lookups of the own domains through it failed."""

    status: ResolverStatus = ResolverStatus.UNKNOWN
    error: str = ""
    lookup_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"status": str(self.status), "error": self.error, "lookup_error": self.lookup_error}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ResolverState":
        """None, as an older agent writes, is `unknown`."""
        if data is None:
            return cls()
        error, lookup_error = data.get("error", ""), data.get("lookup_error", "")
        if not isinstance(error, str) or not isinstance(lookup_error, str):
            raise RelayFormatError("Resolver errors must be strings.")
        status = _choice(ResolverStatus, data.get("status"), "resolver status")
        return cls(status, error, lookup_error)


@dataclass(frozen=True)
class SwitchRequest:
    """The panel asks the agent to move a group to one of its members, for server maintenance.

    The agent takes the file once and moves the group only if the member answers a probe;
    its answer comes back in the state file under the same `id`.
    """

    id: str
    port: int
    backend: Backend

    def __post_init__(self) -> None:
        _check_port(self.port)

    def to_json(self) -> str:
        data = {
            "version": FORMAT_VERSION,
            "id": self.id,
            "port": self.port,
            "backend": self.backend.to_dict(),
        }
        return json.dumps(data) + "\n"


class SwitchOutcome(StrEnum):
    SWITCHED = "switched"
    ALREADY_ACTIVE = "already_active"
    UNREACHABLE = "unreachable"
    UNKNOWN_GROUP = "unknown_group"
    UNKNOWN_BACKEND = "unknown_backend"


@dataclass(frozen=True)
class SwitchResult:
    """The agent's answer to the last switch request it took."""

    id: str
    outcome: SwitchOutcome

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SwitchResult":
        try:
            outcome = SwitchOutcome(_string(data, "outcome"))
        except ValueError as error:
            raise RelayFormatError(f"Unknown switch outcome: {error}") from error
        return cls(_string(data, "id"), outcome)


@dataclass(frozen=True)
class RelayState:
    """The agent's state file: when it was written, the state of every group, the answer to
    the last switch request and the status of the resolver; an agent of an older release
    writes no answer and no resolver."""

    updated_at: datetime
    groups: tuple[GroupState, ...]
    switch: SwitchResult | None = None
    resolver: ResolverState = field(default_factory=ResolverState)

    @classmethod
    def parse(cls, text: str) -> "RelayState":
        data = _document(text, "relay state")
        switch = data.get("switch")
        if switch is not None and not isinstance(switch, dict):
            raise RelayFormatError("'switch' must be an object or null.")
        resolver = data.get("resolver")
        if resolver is not None and not isinstance(resolver, dict):
            raise RelayFormatError("'resolver' must be an object or null.")
        return _parsed(
            lambda: cls(
                updated_at=_required_moment(data, "updated_at"),
                groups=tuple(GroupState.from_dict(item) for item in _objects(data, "groups")),
                switch=SwitchResult.from_dict(switch) if switch is not None else None,
                resolver=ResolverState.from_dict(resolver),
            )
        )

    def group(self, port: int) -> GroupState | None:
        return next((group for group in self.groups if group.port == port), None)


def _document(text: str, name: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise RelayFormatError(f"The {name} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise RelayFormatError(f"The {name} must be a JSON object.")
    if data.get("version") != FORMAT_VERSION:
        raise RelayFormatError(f"The {name} has unknown version {data.get('version')!r}.")
    return data


def _parsed[T](build: Callable[[], T]) -> T:
    """Run a parser; a bad key inside, e.g. in the service peer, becomes RelayFormatError."""
    try:
        return build()
    except RelayFormatError:
        raise
    except AmneziaFormatError as error:
        raise RelayFormatError(str(error)) from error


def _check_port(port: int) -> None:
    if not 1 <= port <= _MAX_PORT:
        raise RelayFormatError(f"Port {port} is out of range.")


def _integer(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RelayFormatError(f"{key!r} must be a non-negative integer.")
    return value


def _optional_integer(data: Mapping[str, Any], key: str) -> int | None:
    return _integer(data, key) if data.get(key) is not None else None


def _string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise RelayFormatError(f"{key!r} must be a string.")
    return value


def _object(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise RelayFormatError(f"{key!r} must be an object.")
    return value


def _objects(data: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RelayFormatError(f"{key!r} must be a list of objects.")
    return value


def _interface(data: Mapping[str, Any], key: str) -> IPv4Interface:
    try:
        return IPv4Interface(_string(data, key))
    except ValueError as error:
        raise RelayFormatError(f"{key!r} is not an IPv4 interface address: {error}") from error


def _address(data: Mapping[str, Any], key: str, default: IPv4Address) -> IPv4Address:
    if key not in data:
        return default
    try:
        return IPv4Address(_string(data, key))
    except ValueError as error:
        raise RelayFormatError(f"{key!r} is not an IPv4 address: {error}") from error


def _addresses(data: Mapping[str, Any], key: str) -> tuple[IPv4Address, ...]:
    values = data.get(key, [])
    if not isinstance(values, list):
        raise RelayFormatError(f"{key!r} must be a list.")
    try:
        return tuple(IPv4Address(str(value)) for value in values)
    except ValueError as error:
        raise RelayFormatError(f"{key!r} has a bad IPv4 address: {error}") from error


def _params(data: Mapping[str, Any]) -> dict[str, str]:
    params = _object(data, "awg_params")
    if not all(isinstance(value, str) for value in params.values()):
        raise RelayFormatError("awg_params must map names to strings.")
    return params


def _choice[E: StrEnum](kind: type[E], value: Any, name: str) -> E:
    try:
        return kind(value)
    except ValueError as error:
        raise RelayFormatError(f"Unknown {name} {value!r}.") from error


def _moment(data: Mapping[str, Any], key: str) -> datetime | None:
    """Unix time in seconds, as any agent writes it with `date +%s`; null when unknown."""
    if data.get(key) is None:
        return None
    return datetime.fromtimestamp(_integer(data, key), UTC)


def _required_moment(data: Mapping[str, Any], key: str) -> datetime:
    moment = _moment(data, key)
    if moment is None:
        raise RelayFormatError(f"{key!r} is required.")
    return moment
