"""Health of a relay's groups as the panel keeps it, and what changed between two readings.

The agent names members by address; the monitor matches them to group members, so the panel
keeps member ids and names. Comparing a reading with the previous one gives the events of
docs/FALLBACK.md section 7: a switch, a member the relay cannot reach, a group with none left.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from amnezia_core.relay import DEFAULT_FAILURES, Backend, BackendState, GroupState, RelayState


@dataclass(frozen=True)
class Member:
    """A group member as the relay knows it: the id and the server name at the reading."""

    id: int
    name: str


@dataclass(frozen=True)
class GroupHealth:
    """One group: its active member, when it last switched, who answers and who does not."""

    port: int
    active: Member | None
    switched_at: int | None
    alive: tuple[Member, ...]
    down: tuple[Member, ...]
    bytes_from_clients: int
    bytes_to_clients: int

    @classmethod
    def of(cls, state: GroupState, members: Mapping[Backend, Member]) -> "GroupHealth":
        """A member the relay probes without the needed failures in a row is still alive.

        An address that no member has now (a member left) is not counted.
        """
        alive = [members[b.backend] for b in state.backends if b.backend in members and _up(b)]
        down = [members[b.backend] for b in state.backends if b.backend in members and _down(b)]
        return cls(
            port=state.port,
            active=members.get(state.active) if state.active else None,
            switched_at=int(state.switched_at.timestamp()) if state.switched_at else None,
            alive=tuple(alive),
            down=tuple(down),
            bytes_from_clients=state.bytes_from_clients,
            bytes_to_clients=state.bytes_to_clients,
        )

    @property
    def all_down(self) -> bool:
        return bool(self.down) and not self.alive

    def reachable(self, member_id: int) -> bool | None:
        """True or False as the relay saw the member; None when it does not probe it."""
        if any(member.id == member_id for member in self.alive):
            return True
        if any(member.id == member_id for member in self.down):
            return False
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "active": vars(self.active) if self.active else None,
            "switched_at": self.switched_at,
            "alive": [vars(member) for member in self.alive],
            "down": [vars(member) for member in self.down],
            "bytes_from_clients": self.bytes_from_clients,
            "bytes_to_clients": self.bytes_to_clients,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GroupHealth":
        active = data.get("active")
        return cls(
            port=int(data["port"]),
            active=Member(**active) if active else None,
            switched_at=data.get("switched_at"),
            alive=tuple(Member(**item) for item in data.get("alive", ())),
            down=tuple(Member(**item) for item in data.get("down", ())),
            bytes_from_clients=int(data.get("bytes_from_clients", 0)),
            bytes_to_clients=int(data.get("bytes_to_clients", 0)),
        )


@dataclass(frozen=True)
class Switched:
    port: int
    before: Member
    after: Member
    at: int


@dataclass(frozen=True)
class MemberDown:
    port: int
    member: Member


@dataclass(frozen=True)
class AllDown:
    port: int


type Change = Switched | MemberDown | AllDown


@dataclass(frozen=True)
class RelayHealth:
    """Every group of one reading and the moment the agent wrote it (Unix seconds)."""

    updated_at: int
    groups: dict[int, GroupHealth] = field(default_factory=dict)

    @classmethod
    def of(
        cls, state: RelayState, members: Mapping[int, Mapping[Backend, Member]]
    ) -> "RelayHealth":
        """`members`: the members of each group port by the address the relay probes."""
        groups = {
            group.port: GroupHealth.of(group, members.get(group.port, {})) for group in state.groups
        }
        return cls(int(state.updated_at.timestamp()), groups)

    @classmethod
    def load(cls, data: Mapping[str, Any]) -> "RelayHealth | None":
        """The stored health; None before the first reading or for a form this code cannot read."""
        try:
            groups = (GroupHealth.from_dict(item) for item in data["groups"])
            return cls(int(data["updated_at"]), {group.port: group for group in groups})
        except (KeyError, TypeError, ValueError):
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "updated_at": self.updated_at,
            "groups": [group.to_dict() for _port, group in sorted(self.groups.items())],
        }

    def changes(self, previous: "RelayHealth | None") -> Iterator[Change]:
        """What happened since `previous`; with none, only the members that are down now.

        The first active member after an installation is no switch.
        """
        for port, group in self.groups.items():
            before = previous.groups.get(port) if previous else None
            if (
                before is not None
                and before.active is not None
                and group.active is not None
                and before.active.id != group.active.id
                and group.switched_at is not None
            ):
                yield Switched(port, before.active, group.active, group.switched_at)
            was_down = {member.id for member in before.down} if before else set()
            for member in group.down:
                if member.id not in was_down:
                    yield MemberDown(port, member)
            if group.all_down and not (before and before.all_down):
                yield AllDown(port)

    def rates(self, previous: "RelayHealth | None") -> tuple[float, float] | None:
        """Bytes per second from and to the clients since `previous`; None when unknown.

        The agent counts from its start, so a smaller number means it restarted.
        """
        if previous is None or self.updated_at <= previous.updated_at:
            return None
        now, then = self._bytes(), previous._bytes()
        if now[0] < then[0] or now[1] < then[1]:
            return None
        seconds = self.updated_at - previous.updated_at
        return (now[0] - then[0]) / seconds, (now[1] - then[1]) / seconds

    def _bytes(self) -> tuple[int, int]:
        groups = self.groups.values()
        return sum(g.bytes_from_clients for g in groups), sum(g.bytes_to_clients for g in groups)


def _down(backend: BackendState) -> bool:
    return not backend.alive and backend.failures >= DEFAULT_FAILURES


def _up(backend: BackendState) -> bool:
    return backend.checked_at is not None and not _down(backend)
