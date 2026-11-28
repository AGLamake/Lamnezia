"""Turning raw peer counters into traffic deltas and hourly totals (DESIGN.md 3.4, 5.5)."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from django.db import transaction

from apps.traffic.models import Shaping, TrafficCounter, TrafficHourly, TrafficSource


class Reading(Protocol):
    """Raw counters of one client: a peer of `awg show dump` or an XRay client."""

    @property
    def public_key(self) -> str: ...

    @property
    def rx_bytes(self) -> int: ...

    @property
    def tx_bytes(self) -> int: ...

    @property
    def latest_handshake(self) -> datetime | None: ...


@dataclass(frozen=True)
class Delta:
    rx_bytes: int
    tx_bytes: int

    def __bool__(self) -> bool:
        return bool(self.rx_bytes or self.tx_bytes)


def counter_delta(
    counter: TrafficCounter, rx_bytes: int, tx_bytes: int, *, restarted: bool = False
) -> Delta:
    """Bytes since the last reading. Counters only grow until the container restarts or the
    peer is re-created; then they start from zero, so all of the new value is new traffic.

    A restart is seen by the counters going down or, when they grew past the old values
    again, by `restarted`: the container's start time changed.
    """
    if restarted or rx_bytes < counter.rx_bytes or tx_bytes < counter.tx_bytes:
        return Delta(rx_bytes, tx_bytes)
    return Delta(rx_bytes - counter.rx_bytes, tx_bytes - counter.tx_bytes)


class TrafficRecorder:
    """Stores readings of one protocol instance (`source`).

    A peer seen for the first time only sets the baseline: on an imported server its counters
    hold traffic from before the panel. Use `expect_peer` for a new key to count from zero.
    On a member of a group read before, a new key of the leading server counts from zero too:
    counters are never deleted, so the peer was added since the last reading and all its bytes
    are new. The first reading of a member stays a baseline.

    A clone in a server group keeps its own counters, but the traffic they add belongs to the
    keys of the leading server (`key_source`): a key counts on every member of its group.
    A peer that is not a key counts for the source its owner names (`targets.PeerOwners`).
    """

    def expect_peer(self, source: str, public_key: str, at: datetime, key_source: str = "") -> None:
        TrafficCounter.objects.get_or_create(
            source=source,
            public_key=public_key,
            defaults={"collected_at": at, "key_source": key_source or source},
        )

    def expect_member_peer(
        self, source: str, public_key: str, at: datetime, key_source: str
    ) -> None:
        """`expect_peer` on a member of a group, only once a reading of it succeeded: the first
        reading of a member is a baseline for all its peers, an expected one among them too."""
        if TrafficSource.objects.filter(source=source, available=True).exists():
            self.expect_peer(source, public_key, at, key_source)

    def record(  # noqa: PLR0913  a reading, its moment and whose traffic it is
        self,
        source: str,
        peers: Sequence[Reading],
        at: datetime,
        started: str = "",
        key_source: str = "",
        *,
        owners: Mapping[str, str] | None = None,
    ) -> bool:
        """Apply a reading taken at `at`; False if a newer reading is already stored.

        `started` is the container's start time, empty when unknown. `key_source` is the source
        of the keys the peers belong to when it is not `source`. `owners` maps the public key of
        a peer that is not a key to the source its traffic counts for.
        """
        key_source = key_source or source
        sources = _Owners(key_source, owners or {})
        with transaction.atomic():
            # Row locks serialize readings of one source, so overlapping runs never add twice.
            state, _created = TrafficSource.objects.select_for_update().get_or_create(
                source=source, defaults={"checked_at": at}
            )
            # Members of a group are read in parallel and add to the same hourly rows.
            for shared in sources.shared(source, peers):
                TrafficSource.objects.select_for_update().get_or_create(
                    source=shared, defaults={"checked_at": at}
                )
            counters = {
                c.public_key: c
                for c in TrafficCounter.objects.select_for_update().filter(source=source)
            }
            if any(c.collected_at >= at for c in counters.values()):
                return False
            restarted = bool(state.started and started and state.started != started)
            fresh = key_source != source and bool(counters or state.started)
            deltas = self._update_counters(
                source, sources, peers, counters, at, restarted=restarted, fresh=fresh
            )
            for owner, owned in sources.split(deltas).items():
                self._add_hourly(owner, owned, at)
            state.available, state.checked_at = True, at
            state.started = started or state.started
            state.key_source = _held(source, key_source)
            state.save(update_fields=["available", "checked_at", "started", "key_source"])
        return True

    def shaping(self, source: str, found: str) -> None:
        """Keep what the reading found of the speed limits of the container (`Shaping`)."""
        if found in Shaping.values:
            TrafficSource.objects.filter(source=source).update(shaping=found)

    def unavailable(self, source: str, at: datetime, key_source: str = "") -> None:
        """The instance gave no reading: its traffic, and that of the keys it holds as a member
        of a group (`key_source`), is unknown until it does."""
        TrafficSource.objects.update_or_create(
            source=source,
            defaults={
                "available": False,
                "checked_at": at,
                "key_source": _held(source, key_source),
            },
        )

    @staticmethod
    def _update_counters(  # noqa: PLR0913 - the counters of a reading and whose keys they count
        source: str,
        sources: "_Owners",
        peers: Sequence[Reading],
        counters: dict[str, TrafficCounter],
        at: datetime,
        *,
        restarted: bool,
        fresh: bool,
    ) -> dict[str, Delta]:
        """`fresh`: a new key of the leading server counts from zero."""
        deltas: dict[str, Delta] = {}
        seen, created = [], []
        for known in counters.values():
            # A server joins or leaves a group between readings.
            known.key_source = sources.of(known.public_key)
        for peer in peers:
            counter = counters.pop(peer.public_key, None)
            if counter is None:
                counter = TrafficCounter(
                    source=source,
                    key_source=sources.of(peer.public_key),
                    public_key=peer.public_key,
                )
                created.append(counter)
                added = Delta(peer.rx_bytes, peer.tx_bytes)
                if fresh and added and sources.is_key(peer.public_key):
                    deltas[peer.public_key] = added
            else:
                seen.append(counter)
                delta = counter_delta(counter, peer.rx_bytes, peer.tx_bytes, restarted=restarted)
                if delta:
                    deltas[peer.public_key] = delta
                    if peer.latest_handshake is None:
                        counter.active_at = at
            counter.rx_bytes, counter.tx_bytes = peer.rx_bytes, peer.tx_bytes
            counter.latest_handshake = peer.latest_handshake or counter.latest_handshake
            counter.present = True
            counter.collected_at = at
        # A peer missing from the server has lost its counters; when it comes back
        # (enabled again), its traffic counts from zero.
        for counter in counters.values():
            counter.rx_bytes = counter.tx_bytes = 0
            counter.present = False
            counter.collected_at = at
        TrafficCounter.objects.bulk_update(
            [*seen, *counters.values()],
            [
                "rx_bytes",
                "tx_bytes",
                "latest_handshake",
                "active_at",
                "present",
                "key_source",
                "collected_at",
            ],
        )
        TrafficCounter.objects.bulk_create(created, ignore_conflicts=True)
        return deltas

    @staticmethod
    def _add_hourly(source: str, deltas: dict[str, Delta], at: datetime) -> None:
        hour = at.replace(minute=0, second=0, microsecond=0)
        rows = {
            row.public_key: row
            for row in TrafficHourly.objects.filter(source=source, hour=hour, public_key__in=deltas)
        }
        for public_key, delta in deltas.items():
            row = rows.setdefault(
                public_key, TrafficHourly(source=source, public_key=public_key, hour=hour)
            )
            row.rx_bytes += delta.rx_bytes
            row.tx_bytes += delta.tx_bytes
        TrafficHourly.objects.bulk_update(
            [r for r in rows.values() if r.pk], ["rx_bytes", "tx_bytes"]
        )
        TrafficHourly.objects.bulk_create([r for r in rows.values() if not r.pk])


def _held(source: str, key_source: str) -> str:
    """The source of the keys a source holds when they are not its own, else empty."""
    return "" if key_source in ("", source) else key_source


@dataclass(frozen=True)
class _Owners:
    """Whose traffic each peer of a reading is: the keys of `key_source`, or another owner."""

    key_source: str
    owners: Mapping[str, str]

    def of(self, public_key: str) -> str:
        return self.owners.get(public_key, self.key_source)

    def is_key(self, public_key: str) -> bool:
        return public_key not in self.owners

    def shared(self, source: str, peers: Sequence[Reading]) -> list[str]:
        """The sources other than `source` the reading adds to, in a fixed order for the locks."""
        others = {self.owners[p.public_key] for p in peers if p.public_key in self.owners}
        return sorted(({self.key_source} | others) - {source})

    def split(self, deltas: Mapping[str, Delta]) -> dict[str, dict[str, Delta]]:
        """The deltas by the source they count for."""
        found: dict[str, dict[str, Delta]] = defaultdict(dict)
        for public_key, delta in deltas.items():
            found[self.of(public_key)][public_key] = delta
        return found
