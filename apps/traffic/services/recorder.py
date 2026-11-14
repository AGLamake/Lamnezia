"""Turning raw peer counters into traffic deltas and hourly totals (DESIGN.md 3.4, 5.5)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from django.db import transaction

from apps.traffic.models import TrafficCounter, TrafficHourly, TrafficSource


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

    A clone in a server group keeps its own counters, but the traffic they add belongs to the
    keys of the leading server (`key_source`): a key counts on every member of its group.
    """

    def expect_peer(self, source: str, public_key: str, at: datetime) -> None:
        TrafficCounter.objects.get_or_create(
            source=source,
            public_key=public_key,
            defaults={"collected_at": at, "key_source": source},
        )

    def record(
        self,
        source: str,
        peers: Sequence[Reading],
        at: datetime,
        started: str = "",
        key_source: str = "",
    ) -> bool:
        """Apply a reading taken at `at`; False if a newer reading is already stored.

        `started` is the container's start time, empty when unknown. `key_source` is the source
        of the keys the peers belong to when it is not `source`.
        """
        key_source = key_source or source
        with transaction.atomic():
            # Row locks serialize readings of one source, so overlapping runs never add twice.
            state, _created = TrafficSource.objects.select_for_update().get_or_create(
                source=source, defaults={"checked_at": at}
            )
            if key_source != source:
                # Members of a group are read in parallel and add to the same hourly rows.
                TrafficSource.objects.select_for_update().get_or_create(
                    source=key_source, defaults={"checked_at": at}
                )
            counters = {
                c.public_key: c
                for c in TrafficCounter.objects.select_for_update().filter(source=source)
            }
            if any(c.collected_at >= at for c in counters.values()):
                return False
            restarted = bool(state.started and started and state.started != started)
            deltas = self._update_counters(
                source, key_source, peers, counters, at, restarted=restarted
            )
            self._add_hourly(key_source, deltas, at)
            state.available, state.checked_at = True, at
            state.started = started or state.started
            state.save(update_fields=["available", "checked_at", "started"])
        return True

    def unavailable(self, source: str, at: datetime) -> None:
        """The instance gave no reading: its traffic is unknown until it does."""
        TrafficSource.objects.update_or_create(
            source=source, defaults={"available": False, "checked_at": at}
        )

    @staticmethod
    def _update_counters(  # noqa: PLR0913 - the counters of a reading and whose keys they count
        source: str,
        key_source: str,
        peers: Sequence[Reading],
        counters: dict[str, TrafficCounter],
        at: datetime,
        *,
        restarted: bool,
    ) -> dict[str, Delta]:
        deltas: dict[str, Delta] = {}
        seen, created = [], []
        for known in counters.values():
            known.key_source = key_source  # a server joins or leaves a group between readings
        for peer in peers:
            counter = counters.pop(peer.public_key, None)
            if counter is None:
                counter = TrafficCounter(
                    source=source, key_source=key_source, public_key=peer.public_key
                )
                created.append(counter)
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
        if not deltas:
            return
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
