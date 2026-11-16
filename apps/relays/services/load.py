"""Channel load history of a relay: the readings averaged per bucket for the chart of its card.

The monitor keeps a reading per poll (`RelaySample`); old readings are thinned like those of
servers (apps/servers/services/thinning.py) and deleted after the history term.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from apps.relays.models import Relay, RelaySample

DAY = timedelta(days=1)
WEEK = timedelta(days=7)


@dataclass(frozen=True)
class LoadSeries:
    """Average bytes per second per bucket from `since`, oldest first; None: no reading in it."""

    since: datetime
    from_clients: list[float | None]
    to_clients: list[float | None]

    @property
    def empty(self) -> bool:
        return all(rate is None for rate in self.from_clients)

    @property
    def peak_from_clients(self) -> float | None:
        return _peak(self.from_clients)

    @property
    def peak_to_clients(self) -> float | None:
        return _peak(self.to_clients)


class LoadHistory:
    """The kept readings of one relay as of `now`."""

    def __init__(self, relay: Relay, now: datetime) -> None:
        self._relay = relay
        self._now = now

    def series(self, period: timedelta, buckets: int) -> LoadSeries:
        """The last `period` split into `buckets` equal parts."""
        since = self._now - period
        width = period / buckets
        sums = [[0.0, 0.0, 0] for _ in range(buckets)]
        readings = self._relay.samples.filter(at__gte=since, at__lte=self._now).values_list(
            "at", "rate_from_clients", "rate_to_clients"
        )
        for at, incoming, outgoing in readings:
            bucket = sums[min(int((at - since) / width), buckets - 1)]
            bucket[0] += incoming
            bucket[1] += outgoing
            bucket[2] += 1
        return LoadSeries(
            since,
            [incoming / count if count else None for incoming, _out, count in sums],
            [outgoing / count if count else None for _in, outgoing, count in sums],
        )


def prune_load(before: datetime) -> None:
    """Readings older than `before` are deleted."""
    RelaySample.objects.filter(at__lt=before).delete()


def _peak(rates: list[float | None]) -> float | None:
    return max((rate for rate in rates if rate is not None), default=None)
