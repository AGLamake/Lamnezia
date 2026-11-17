"""What the relay forwards that the panel did not give it, and what it lacks: groups and members.

The agent writes the groups of its config.json into state.json, so a config that someone else
replaced shows in the state: a group the panel does not have, a group of the panel missing, or
other members in a group. The monitor compares a reading with what the panel would write
(services/config.py) and keeps the result for the relay card.
"""

from collections.abc import Mapping
from dataclasses import dataclass
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
    panel the relay does not forward (`missing_ports`) and groups with other members.

    It is false when the relay forwards exactly what the panel would write.
    """

    foreign_ports: tuple[int, ...] = ()
    missing_ports: tuple[int, ...] = ()
    groups: tuple[GroupMismatch, ...] = ()

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

    def __bool__(self) -> bool:
        return bool(self.foreign_ports or self.missing_ports or self.groups)

    def to_dict(self) -> dict[str, Any]:
        """The stored form; an empty dict when nothing differs."""
        if not self:
            return {}
        return {
            "foreign_ports": list(self.foreign_ports),
            "missing_ports": list(self.missing_ports),
            "groups": [group.to_dict() for group in self.groups],
        }

    @classmethod
    def load(cls, data: Mapping[str, Any]) -> "RelayMismatch":
        """The stored mismatch; none for an empty dict or a form this code cannot read."""
        try:
            return cls(
                foreign_ports=tuple(int(port) for port in data.get("foreign_ports", ())),
                missing_ports=tuple(int(port) for port in data.get("missing_ports", ())),
                groups=tuple(GroupMismatch.from_dict(item) for item in data.get("groups", ())),
            )
        except (KeyError, TypeError, ValueError):
            return cls()


def _sorted(backends: set[Backend]) -> tuple[Backend, ...]:
    return tuple(sorted(backends, key=lambda backend: (backend.host, backend.port)))
