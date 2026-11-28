"""Channel load history of a relay and traffic history of split groups: the readings averaged
per bucket for the charts of the relay card and the group page.

The monitor keeps a reading per poll (`RelaySample`, `SplitSample`); old readings are thinned
like those of servers (apps/servers/services/thinning.py) and deleted after the history term.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import QuerySet

from apps.relays.models import Relay, RelaySample, SplitSample

DAY = timedelta(days=1)
WEEK = timedelta(days=7)

type Averages = list[float | None]


@dataclass(frozen=True)
class LoadSeries:
    """Average bytes per second per bucket from `since`, oldest first; None: no reading in it."""

    since: datetime
    from_clients: Averages
    to_clients: Averages

    @property
    def empty(self) -> bool:
        return all(rate is None for rate in self.from_clients)

    @property
    def peak_from_clients(self) -> float | None:
        return _peak(self.from_clients)

    @property
    def peak_to_clients(self) -> float | None:
        return _peak(self.to_clients)


@dataclass(frozen=True)
class SplitSeries:
    """Average bytes per second of the clients of split groups per bucket from `since`, both
    directions together, oldest first: abroad through the channel and straight out of the
    relay; None: no reading in it."""

    since: datetime
    abroad: Averages
    direct: Averages

    @property
    def empty(self) -> bool:
        return all(rate is None for rate in self.abroad)

    @property
    def peak_abroad(self) -> float | None:
        return _peak(self.abroad)

    @property
    def peak_direct(self) -> float | None:
        return _peak(self.direct)


class LoadHistory:
    """The kept readings of one relay as of `now`."""

    def __init__(self, relay: Relay, now: datetime) -> None:
        self._relay = relay
        self._now = now

    def series(self, period: timedelta, buckets: int) -> LoadSeries:
        """The last `period` split into `buckets` equal parts."""
        since = self._now - period
        readings = self._relay.samples.filter(at__gte=since, at__lte=self._now).values_list(
            "at", "rate_from_clients", "rate_to_clients"
        )
        return LoadSeries(since, *_averages(readings, since, period / buckets, buckets))


class SplitHistory:
    """The kept readings of some split groups (`samples`) as of `now`; the groups of one reading
    add up."""

    def __init__(self, samples: "QuerySet[SplitSample]", now: datetime) -> None:
        self._samples = samples
        self._now = now

    @classmethod
    def of_relay(cls, relay: Relay, now: datetime) -> "SplitHistory":
        return cls(SplitSample.objects.filter(group__relay=relay.server_id), now)

    def series(self, period: timedelta, buckets: int) -> SplitSeries:
        """The last `period` split into `buckets` equal parts."""
        since = self._now - period
        totals: dict[datetime, list[float]] = defaultdict(lambda: [0.0, 0.0])
        found = self._samples.filter(at__gte=since, at__lte=self._now).values_list(
            "at", "rate_abroad", "rate_direct"
        )
        for at, abroad, direct in found:
            totals[at][0] += abroad
            totals[at][1] += direct
        readings = ((at, abroad, direct) for at, (abroad, direct) in totals.items())
        return SplitSeries(since, *_averages(readings, since, period / buckets, buckets))


def prune_load(before: datetime) -> None:
    """Readings older than `before` are deleted."""
    RelaySample.objects.filter(at__lt=before).delete()
    SplitSample.objects.filter(at__lt=before).delete()


def _averages(
    readings: Iterable[tuple[datetime, float, float]],
    since: datetime,
    width: timedelta,
    buckets: int,
) -> tuple[Averages, Averages]:
    """The average of each of the two rates per bucket of `width` from `since`."""
    sums = [[0.0, 0.0, 0] for _ in range(buckets)]
    for at, first, second in readings:
        bucket = sums[min(int((at - since) / width), buckets - 1)]
        bucket[0] += first
        bucket[1] += second
        bucket[2] += 1
    return (
        [first / count if count else None for first, _second, count in sums],
        [second / count if count else None for _first, second, count in sums],
    )


def _peak(rates: Averages) -> float | None:
    return max((rate for rate in rates if rate is not None), default=None)
