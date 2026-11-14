"""Config of the fallback relay and the state file its agent writes (docs/FALLBACK.md section 5).

The panel writes the config, the agent on the relay reads it and works on its own: it probes the
members of each group with a new handshake of the service peer and moves the group port to the
next backend by priority after `failures` failed probes in a row. The agent writes what it sees
into the state file, which the panel reads over SSH. Both files are JSON with a `version`, so an
agent and a panel of different releases notice a format they do not know.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv4Interface
from typing import Any

from amnezia_core.errors import AmneziaFormatError, RelayFormatError
from amnezia_core.keys import decode_key

FORMAT_VERSION = 1
# Measured in P12-01 (docs/research/relay-fallback.md): a failure is seen in about 14 s, which
# does not lengthen the 18-22 s outage the client has anyway.
DEFAULT_CHECK_INTERVAL = 5
DEFAULT_FAILURES = 3

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
        params = _object(data, "awg_params")
        if not all(isinstance(value, str) for value in params.values()):
            raise RelayFormatError("Service peer awg_params must map names to strings.")
        try:
            address = IPv4Interface(_string(data, "address"))
        except ValueError as error:
            raise RelayFormatError(f"Service peer address is invalid: {error}") from error
        return cls(
            private_key=_string(data, "private_key"),
            address=address,
            server_public_key=_string(data, "server_public_key"),
            preshared_key=_string(data, "preshared_key"),
            awg_params=params,
        )


@dataclass(frozen=True)
class RelayGroup:
    """A group behind the relay: its public UDP port and the members in priority order."""

    port: int
    backends: tuple[Backend, ...]
    service: ServicePeer

    def __post_init__(self) -> None:
        _check_port(self.port)
        if not self.backends:
            raise RelayFormatError(f"Group on port {self.port} has no backends.")
        if len(set(self.backends)) != len(self.backends):
            raise RelayFormatError(f"Group on port {self.port} lists a backend twice.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "backends": [backend.to_dict() for backend in self.backends],
            "service": self.service.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RelayGroup":
        return cls(
            port=_integer(data, "port"),
            backends=tuple(Backend.from_dict(item) for item in _objects(data, "backends")),
            service=ServicePeer.from_dict(_object(data, "service")),
        )


@dataclass(frozen=True)
class RelayConfig:
    """Everything the agent needs: groups, how often to probe and after how many failures to switch.

    The file holds the service peers' private keys: the panel writes it readable by root only.
    """

    groups: tuple[RelayGroup, ...]
    check_interval: int = DEFAULT_CHECK_INTERVAL
    failures: int = DEFAULT_FAILURES

    def __post_init__(self) -> None:
        if self.check_interval < 1 or self.failures < 1:
            raise RelayFormatError("check_interval and failures must be at least 1.")
        ports = [group.port for group in self.groups]
        if len(set(ports)) != len(ports):
            raise RelayFormatError("Two groups of the relay share a port.")

    def to_json(self) -> str:
        data = {
            "version": FORMAT_VERSION,
            "check_interval": self.check_interval,
            "failures": self.failures,
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
            )
        )


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
class GroupState:
    """State of one group: the active backend, the last switch and the forwarded bytes."""

    port: int
    active: Backend | None
    switched_at: datetime | None
    backends: tuple[BackendState, ...]
    bytes_from_clients: int
    bytes_to_clients: int

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GroupState":
        active = data.get("active")
        if active is not None and not isinstance(active, dict):
            raise RelayFormatError("Group state active must be a backend or null.")
        return cls(
            port=_integer(data, "port"),
            active=Backend.from_dict(active) if active is not None else None,
            switched_at=_moment(data, "switched_at"),
            backends=tuple(BackendState.from_dict(item) for item in _objects(data, "backends")),
            bytes_from_clients=_integer(data, "bytes_from_clients"),
            bytes_to_clients=_integer(data, "bytes_to_clients"),
        )


@dataclass(frozen=True)
class RelayState:
    """The agent's state file: when it was written and the state of every group."""

    updated_at: datetime
    groups: tuple[GroupState, ...]

    @classmethod
    def parse(cls, text: str) -> "RelayState":
        data = _document(text, "relay state")
        return _parsed(
            lambda: cls(
                updated_at=_required_moment(data, "updated_at"),
                groups=tuple(GroupState.from_dict(item) for item in _objects(data, "groups")),
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
