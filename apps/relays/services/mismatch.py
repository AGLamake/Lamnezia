"""What the relay forwards that the panel did not give it, and what it lacks: groups and members.

The agent writes the groups of its config.json into state.json, so a config that someone else
replaced shows in the state: a group the panel does not have, a group of the panel missing, or
other members in a group. The container may also publish other UDP ports than the groups have,
when it was started by hand or an update stopped half way: the state then matches, but clients
of a group whose port is not published do not get through. The monitor compares a reading and
the published ports with what the panel would write (services/config.py) and keeps the result
for the relay card.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from amnezia_core.relay import Backend, RelayConfig, RelayState


@dataclass(frozen=True)
class GroupMismatch:
    """One group on both sides with other members: `foreign` are on the relay only,
    `missing` are in the panel only."""

    port: int
    foreign: tuple[Backend, ...] = ()
    missing: tuple[Backend, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "foreign": [backend.to_dict() for backend in self.foreign],
            "missing": [backend.to_dict() for backend in self.missing],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GroupMismatch":
        return cls(
            port=int(data["port"]),
            foreign=tuple(Backend.from_dict(item) for item in data.get("foreign", ())),
            missing=tuple(Backend.from_dict(item) for item in data.get("missing", ())),
        )


@dataclass(frozen=True)
class RelayMismatch:
    """Group ports the relay forwards but the panel does not (`foreign_ports`), ports of the
    panel the relay does not forward (`missing_ports`), groups with other members, ports of the
    panel the container does not publish (`unpublished_ports`) and ports it publishes for no
    group (`extra_published_ports`).

    It is false when the relay forwards and publishes exactly what the panel would write.
    """

    foreign_ports: tuple[int, ...] = ()
    missing_ports: tuple[int, ...] = ()
    groups: tuple[GroupMismatch, ...] = ()
    unpublished_ports: tuple[int, ...] = ()
    extra_published_ports: tuple[int, ...] = ()

    @classmethod
    def of(cls, config: RelayConfig, state: RelayState) -> "RelayMismatch":
        wanted = {group.port: set(group.backends) for group in config.groups}
        found = {group.port: {b.backend for b in group.backends} for group in state.groups}
        groups = (
            GroupMismatch(
                port, _sorted(found[port] - wanted[port]), _sorted(wanted[port] - found[port])
            )
            for port in sorted(wanted.keys() & found.keys())
            if found[port] != wanted[port]
        )
        return cls(
            foreign_ports=tuple(sorted(found.keys() - wanted.keys())),
            missing_ports=tuple(sorted(wanted.keys() - found.keys())),
            groups=tuple(groups),
        )

    def with_published(self, wanted: Iterable[int], published: Iterable[int]) -> "RelayMismatch":
        """This mismatch with the ports the container publishes compared to `wanted`, the ports
        of the panel's groups; their order does not matter."""
        wanted, published = set(wanted), set(published)
        return replace(
            self,
            unpublished_ports=tuple(sorted(wanted - published)),
            extra_published_ports=tuple(sorted(published - wanted)),
        )

    def __bool__(self) -> bool:
        return bool(
            self.foreign_ports
            or self.missing_ports
            or self.groups
            or self.unpublished_ports
            or self.extra_published_ports
        )

    def to_dict(self) -> dict[str, Any]:
        """The stored form; an empty dict when nothing differs."""
        if not self:
            return {}
        return {
            "foreign_ports": list(self.foreign_ports),
            "missing_ports": list(self.missing_ports),
            "groups": [group.to_dict() for group in self.groups],
            "unpublished_ports": list(self.unpublished_ports),
            "extra_published_ports": list(self.extra_published_ports),
        }

    @classmethod
    def load(cls, data: Mapping[str, Any]) -> "RelayMismatch":
        """The stored mismatch; none for an empty dict or a form this code cannot read."""
        try:
            return cls(
                foreign_ports=_ports(data, "foreign_ports"),
                missing_ports=_ports(data, "missing_ports"),
                groups=tuple(GroupMismatch.from_dict(item) for item in data.get("groups", ())),
                unpublished_ports=_ports(data, "unpublished_ports"),
                extra_published_ports=_ports(data, "extra_published_ports"),
            )
        except (KeyError, TypeError, ValueError):
            return cls()


def _ports(data: Mapping[str, Any], key: str) -> tuple[int, ...]:
    """A mismatch stored before the published ports were compared has none of them."""
    return tuple(int(port) for port in data.get(key, ()))


def _sorted(backends: set[Backend]) -> tuple[Backend, ...]:
    return tuple(sorted(backends, key=lambda backend: (backend.host, backend.port)))
