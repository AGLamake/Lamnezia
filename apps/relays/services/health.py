"""Health of a relay's groups as the panel keeps it, and what changed between two readings.

The agent names members by address; the monitor matches them to group members, so the panel
keeps member ids and names. Comparing a reading with the previous one gives the events of
docs/FALLBACK.md section 7: a switch, a member the relay cannot reach, a group with none left,
and the pairs of the last two: the member and the group reachable again.

A member the relay cannot reach opens an outage that goes on from reading to reading. It ends
only after the member has failed no probe seen for `STEADY_SECONDS`: a member that answers one
probe in a few would otherwise be told down and up again every few minutes.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from amnezia_core.relay import DEFAULT_FAILURES, Backend, BackendState, GroupState, RelayState

# How long a member that was down must answer every probe seen before its outage is over.
STEADY_SECONDS = 5 * 60


@dataclass(frozen=True)
class Member:
    """A group member as the relay knows it: the id and the server name at the reading."""

    id: int
    name: str


@dataclass(frozen=True)
class Outage:
    """A member the relay lost: since when, and the last reading in which it failed a probe."""

    member: Member
    since: int
    failed_at: int

    def to_dict(self) -> dict[str, Any]:
        return {"member": vars(self.member), "since": self.since, "failed_at": self.failed_at}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Outage":
        return cls(Member(**data["member"]), int(data["since"]), int(data["failed_at"]))


@dataclass(frozen=True)
class GroupHealth:
    """One group: its active member, when it last switched, who answers and who does not.

    `unanswered`: the active member answers the relay but none of the clients.
    `outages`: the members lost and not steady again yet (see the module docstring).
    """

    port: int
    active: Member | None
    switched_at: int | None
    alive: tuple[Member, ...]
    down: tuple[Member, ...]
    bytes_from_clients: int
    bytes_to_clients: int
    unanswered: bool = False
    outages: tuple[Outage, ...] = ()

    @classmethod
    def of(
        cls,
        state: GroupState,
        members: Mapping[Backend, Member],
        before: "GroupHealth | None" = None,
        now: int = 0,
    ) -> "GroupHealth":
        """A member the relay probes without the needed failures in a row is still alive.

        An address that no member has now (a member left) is not counted. `before` is the
        previous reading of the group, whose outages go on; `now` is the moment of this one.
        """
        known = [(members[b.backend], b) for b in state.backends if b.backend in members]
        alive = [member for member, b in known if _up(b)]
        down = [member for member, b in known if _down(b)]
        failed = {member.id for member, b in known if not _answered(b)}
        others = [member for member, b in known if not _down(b)]
        return cls(
            port=state.port,
            active=members.get(state.active) if state.active else None,
            switched_at=int(state.switched_at.timestamp()) if state.switched_at else None,
            alive=tuple(alive),
            down=tuple(down),
            bytes_from_clients=state.bytes_from_clients,
            bytes_to_clients=state.bytes_to_clients,
            unanswered=state.unanswered,
            outages=_outages(down, others, failed, before, now),
        )

    @property
    def all_down(self) -> bool:
        return bool(self.down) and not self.alive

    def lost(self) -> set[int]:
        """Ids of the members with an outage going on."""
        return {outage.member.id for outage in self.outages}

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
            "unanswered": self.unanswered,
            "outages": [outage.to_dict() for outage in self.outages],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], updated_at: int) -> "GroupHealth":
        """A reading stored before outages were kept has one for each member down then."""
        active = data.get("active")
        down = tuple(Member(**item) for item in data.get("down", ()))
        if "outages" in data:
            outages = tuple(Outage.from_dict(item) for item in data["outages"])
        else:
            outages = tuple(Outage(member, updated_at, updated_at) for member in down)
        return cls(
            port=int(data["port"]),
            active=Member(**active) if active else None,
            switched_at=data.get("switched_at"),
            alive=tuple(Member(**item) for item in data.get("alive", ())),
            down=down,
            bytes_from_clients=int(data.get("bytes_from_clients", 0)),
            bytes_to_clients=int(data.get("bytes_to_clients", 0)),
            unanswered=bool(data.get("unanswered", False)),
            outages=outages,
        )


@dataclass(frozen=True)
class Switched:
    port: int
    before: Member
    after: Member
    at: int


@dataclass(frozen=True)
class MemberDown:
    """A member the relay lost, once per outage."""

    port: int
    member: Member


@dataclass(frozen=True)
class AllDown:
    port: int


@dataclass(frozen=True)
class MemberUp:
    """The outage of a member is over: it has answered steadily since."""

    port: int
    member: Member


@dataclass(frozen=True)
class GroupUp:
    """A group that had no member left has one that answers again."""

    port: int


type Change = Switched | MemberDown | AllDown | MemberUp | GroupUp


@dataclass(frozen=True)
class RelayHealth:
    """Every group of one reading and the moment the agent wrote it (Unix seconds)."""

    updated_at: int
    groups: dict[int, GroupHealth] = field(default_factory=dict)

    @classmethod
    def of(
        cls,
        state: RelayState,
        members: Mapping[int, Mapping[Backend, Member]],
        previous: "RelayHealth | None" = None,
    ) -> "RelayHealth":
        """`members`: the members of each group port by the address the relay probes;
        `previous`: the reading before, whose outages go on."""
        now = int(state.updated_at.timestamp())
        before = previous.groups if previous else {}
        groups = {
            group.port: GroupHealth.of(
                group, members.get(group.port, {}), before.get(group.port), now
            )
            for group in state.groups
        }
        return cls(now, groups)

    @classmethod
    def load(cls, data: Mapping[str, Any]) -> "RelayHealth | None":
        """The stored health; None before the first reading or for a form this code cannot read."""
        try:
            updated_at = int(data["updated_at"])
            groups = (GroupHealth.from_dict(item, updated_at) for item in data["groups"])
            return cls(updated_at, {group.port: group for group in groups})
        except (KeyError, TypeError, ValueError):
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "updated_at": self.updated_at,
            "groups": [group.to_dict() for _port, group in sorted(self.groups.items())],
        }

    def changes(self, previous: "RelayHealth | None") -> Iterator[Change]:
        """What happened since `previous`; with none, only the members that are down now.

        The first active member after an installation is no switch. A member is told down when
        its outage opens and up when it ends while the member is still in the group; a group
        comes up only after it was down in `previous`. So each up follows the down told before.
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
            was_lost, lost = before.lost() if before else set(), group.lost()
            for outage in group.outages:
                if outage.member.id not in was_lost:
                    yield MemberDown(port, outage.member)
            if group.all_down and not (before and before.all_down):
                yield AllDown(port)
            for member in group.alive:
                if member.id in was_lost and member.id not in lost:
                    yield MemberUp(port, member)
            if before is not None and before.all_down and group.alive:
                yield GroupUp(port)

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


def _outages(
    down: list[Member],
    others: list[Member],
    failed: set[int],
    before: GroupHealth | None,
    now: int,
) -> tuple[Outage, ...]:
    """Each member down opens an outage or goes on with its own; any other member keeps its
    outage until it has failed no probe seen for `STEADY_SECONDS` (one not probed yet has not
    answered). A member that left drops its own."""
    going = {outage.member.id: outage for outage in before.outages} if before else {}
    outages = [
        replace(going[m.id], member=m, failed_at=now) if m.id in going else Outage(m, now, now)
        for m in down
    ]
    for member in others:
        outage = going.get(member.id)
        if outage is None:
            continue
        failed_at = now if member.id in failed else outage.failed_at
        if now - failed_at < STEADY_SECONDS:
            outages.append(replace(outage, member=member, failed_at=failed_at))
    return tuple(outages)


def _answered(backend: BackendState) -> bool:
    """The last probe of the member got an answer."""
    return backend.alive and backend.checked_at is not None


def _down(backend: BackendState) -> bool:
    return not backend.alive and backend.failures >= DEFAULT_FAILURES


def _up(backend: BackendState) -> bool:
    return backend.checked_at is not None and not _down(backend)
