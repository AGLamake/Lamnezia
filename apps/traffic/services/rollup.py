"""Folding old hourly traffic into one row per peer and local day, totals and limits unchanged.

A folded row keeps the hour of its first row. Limits count rows by `hour >= period start`, so a
day is cut where a period of the key owner starts in it (the plan was assigned or a new period
began): the hours on each side stay on their side. Statistics sum local days, as the fold does.
"""

from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.keys.models import AccessKey
from apps.plans.models import UserLimits
from apps.plans.services.limits import EffectiveLimits
from apps.traffic.models import TrafficHourly

Peer = tuple[str, str]


class TrafficRollup:
    """Folds the hours of whole local days older than `keep`; at least a day stays hourly for
    the statistics of the last day."""

    def __init__(self, keep: timedelta, now: datetime) -> None:
        self._before = _day_start(timezone.localdate(now - max(keep, timedelta(days=1))))
        self._owners: dict[Peer, list[int]] = defaultdict(list)
        self._limits: dict[int, EffectiveLimits] = {}

    def run(self) -> int:
        """Fold every day that still has several rows of a peer; return how many rows went."""
        days = self._days_to_fold()
        if not days:
            return 0
        self._load_owners()
        return sum(self._fold_peer(peer, first) for peer, first in days.items())

    def _days_to_fold(self) -> dict[Peer, date]:
        """The earliest day of every peer with more than one row in it."""
        groups = (
            TrafficHourly.objects.filter(hour__lt=self._before)
            .annotate(day=TruncDate("hour"))
            .values("source", "public_key", "day")
            .annotate(rows=Count("id"))
            .filter(rows__gt=1)
        )
        earliest: dict[Peer, date] = {}
        for group in groups:
            peer = (group["source"], group["public_key"])
            earliest[peer] = min(group["day"], earliest.get(peer, group["day"]))
        return earliest

    def _load_owners(self) -> None:
        for source, public_key, user_id in AccessKey.objects.filter(user__isnull=False).values_list(
            "source", "public_key", "user_id"
        ):
            self._owners[(source, public_key)].append(user_id)
        subscriptions = (
            UserLimits.objects.filter(plan__isnull=False)
            .select_related("plan")
            .prefetch_related("plan__servers")
        )
        self._limits = {limits.user_id: EffectiveLimits.of(limits) for limits in subscriptions}

    @transaction.atomic
    def _fold_peer(self, peer: Peer, first: date) -> int:
        rows = TrafficHourly.objects.filter(
            source=peer[0], public_key=peer[1], hour__gte=_day_start(first), hour__lt=self._before
        ).order_by("hour")
        cuts = self._cuts(peer, _day_start(first))
        kept: list[TrafficHourly] = []
        extra: list[int] = []
        for segment in _segments(rows, cuts):
            head, *rest = segment
            if not rest:
                continue
            head.rx_bytes = sum(row.rx_bytes for row in segment)
            head.tx_bytes = sum(row.tx_bytes for row in segment)
            kept.append(head)
            extra.extend(row.pk for row in rest)
        TrafficHourly.objects.bulk_update(kept, ["rx_bytes", "tx_bytes"])
        TrafficHourly.objects.filter(pk__in=extra).delete()
        return len(extra)

    def _cuts(self, peer: Peer, start: datetime) -> list[datetime]:
        """Period starts of the owners from `start` to the end of the folded days."""
        cuts: set[datetime] = set()
        for user_id in self._owners.get(peer, []):
            limits = self._limits.get(user_id)
            if limits is not None:
                cuts.update(_period_starts(limits, start, self._before))
        return sorted(cuts)


def _segments(rows: Iterable[TrafficHourly], cuts: list[datetime]) -> Iterator[list[TrafficHourly]]:
    """Rows grouped by local day and split at the cuts; every group is in hour order."""
    segment: list[TrafficHourly] = []
    key: tuple[date, int] | None = None
    for row in rows:
        row_key = (timezone.localdate(row.hour), sum(cut <= row.hour for cut in cuts))
        if segment and row_key != key:
            yield segment
            segment = []
        segment.append(row)
        key = row_key
    if segment:
        yield segment


def _period_starts(limits: EffectiveLimits, start: datetime, end: datetime) -> Iterator[datetime]:
    if limits.period_start is None:
        return
    length = timedelta(days=max(limits.period_days, 1))
    moment = limits.period_start
    if moment < start:
        moment -= (moment - start) // length * length
    while moment < end:
        yield moment
        moment += length


def _day_start(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time()))
