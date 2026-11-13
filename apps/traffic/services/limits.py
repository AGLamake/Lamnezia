"""Applying the limits of plans (DESIGN.md 5.5): over a limit or past the access term the keys of a
user go off; in a new period or after an extension they go back on.

Only users with a plan are checked. Keys turned off by hand are never turned on here.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import batched

from django.db import transaction
from django.db.models import (
    Case,
    DateTimeField,
    Exists,
    F,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.dispatch import Signal

from apps.accounts.models import User
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.limits import disable_by_limit, enable_by_limit
from apps.operations.models import Operation
from apps.plans.models import UserLimits
from apps.plans.services.limits import EffectiveLimits
from apps.traffic.models import TrafficHourly
from apps.traffic.signals import LimitReason, access_restored, limit_reached

# Users per usage query: the starts of each one are written into the SQL.
_CHUNK = 1000
# The start of a count over all readings.
_EVER = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class _Subscriber:
    """A user with a plan and keys the limits may switch, and which of those keys there are."""

    user: User
    limits: EffectiveLimits
    has_active: bool
    has_limited: bool

    @classmethod
    def all(cls) -> list["_Subscriber"]:
        keys = AccessKey.objects.filter(user=OuterRef("user"))
        rows = (
            UserLimits.objects.filter(plan__isnull=False)
            .annotate(
                has_active=Exists(keys.filter(status=KeyStatus.ACTIVE)),
                has_limited=Exists(keys.filter(status=KeyStatus.DISABLED_LIMIT)),
            )
            .filter(Q(has_active=True) | Q(has_limited=True))
            .select_related("plan", "user")
            .prefetch_related("plan__servers")
        )
        return [
            cls(row.user, EffectiveLimits.of(row), row.has_active, row.has_limited) for row in rows
        ]


class LimitEnforcer:
    """Traffic counts both directions of every key the user owns, revoked keys too."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def enforce_all(self) -> None:
        """The same few queries for any number of users; only keys that change add more."""
        subscribers = _Subscriber.all()
        counted = {s.user.pk: s.limits for s in subscribers if _counts_traffic(s.limits)}
        usage = self.usages(counted)
        for subscriber in subscribers:
            reason = self._reason(subscriber.limits, usage.get(subscriber.user.pk, (0, 0)))
            if subscriber.has_limited if reason is None else subscriber.has_active:
                self.switch(subscriber.user, reason)

    def switch(self, user: User, reason: LimitReason | None) -> list[Operation]:
        """Turn the keys off for a reached limit or back on for none; return what started."""
        if reason is None:
            started = enable_by_limit(user)
            self._notify(started, access_restored, user)
        else:
            started = disable_by_limit(user, reason)
            self._notify(started, limit_reached, user, reason=reason)
        return started

    def reason(self, user: User, limits: EffectiveLimits) -> LimitReason | None:
        usage = self.usage(user, limits) if _counts_traffic(limits) else (0, 0)
        return self._reason(limits, usage)

    def usage(self, user: User, limits: EffectiveLimits) -> tuple[int, int]:
        """Bytes in the current period and since the plan was assigned."""
        return self.usages({user.pk: limits}).get(user.pk, (0, 0))

    def usages(self, limits: dict[int, EffectiveLimits]) -> dict[int, tuple[int, int]]:
        """`usage` of every user id given, in one query per thousand users.

        Every key gets its owner's starts, then sums its own hours from each: a range of the peer
        index per key. Readings are hourly, so the hour a period starts in counts for the period
        before. Revoked keys count too.
        """
        used: dict[int, tuple[int, int]] = {}
        for chunk in batched(limits.items(), _CHUNK):
            period = _starts((uid, lim.current_period_start(self._now)) for uid, lim in chunk)
            total = _starts((uid, lim.period_start) for uid, lim in chunk)
            keys = (
                AccessKey.objects.filter(user_id__in=[uid for uid, _lim in chunk])
                .annotate(period_start=period, total_start=total)
                .annotate(period=_sum_since("period_start"), total=_sum_since("total_start"))
                .values_list("user_id", "period", "total")
            )
            for uid, in_period, in_total in keys:
                before = used.get(uid, (0, 0))
                used[uid] = (before[0] + (in_period or 0), before[1] + (in_total or 0))
        return used

    def _reason(self, limits: EffectiveLimits, usage: tuple[int, int]) -> LimitReason | None:
        if limits.is_expired(self._now):
            return LimitReason.EXPIRED
        period, total = usage
        if limits.total_traffic_limit is not None and total >= limits.total_traffic_limit:
            return LimitReason.TOTAL_TRAFFIC
        if limits.traffic_limit is not None and period >= limits.traffic_limit:
            return LimitReason.TRAFFIC
        return None

    @staticmethod
    def _notify(started: list[Operation], signal: Signal, user: User, **data: str) -> None:
        """Only a run that started changes notifies, so a user hears once per change."""
        if started:
            transaction.on_commit(lambda: signal.send(LimitEnforcer, user=user, **data))


def traffic_by_direction(user: User, since: datetime | None) -> tuple[int, int]:
    """Bytes the server received and sent for all the user's keys, revoked too, since the hour of
    `since` (`None`: every reading). Received is the client's upload.
    """
    keys = AccessKey.objects.filter(
        user=user, source=OuterRef("source"), public_key=OuterRef("public_key")
    )
    totals = TrafficHourly.objects.filter(Exists(keys), hour__gte=since or _EVER).aggregate(
        rx=Sum("rx_bytes"), tx=Sum("tx_bytes")
    )
    return totals["rx"] or 0, totals["tx"] or 0


def _counts_traffic(limits: EffectiveLimits) -> bool:
    return limits.traffic_limit is not None or limits.total_traffic_limit is not None


def _starts(pairs: Iterable[tuple[int, datetime | None]]) -> Case:
    """The start of each user's count, for the keys they own; `None` counts every reading."""
    whens = [When(user_id=uid, then=Value(start or _EVER)) for uid, start in pairs]
    return Case(*whens, output_field=DateTimeField())


def _sum_since(start: str) -> Subquery:
    """Both directions of the key's readings from the hour in its `start` annotation."""
    readings = TrafficHourly.objects.filter(
        source=OuterRef("source"), public_key=OuterRef("public_key"), hour__gte=OuterRef(start)
    )
    summed = readings.values("source", "public_key").annotate(
        used=Sum(F("rx_bytes") + F("tx_bytes"))
    )
    return Subquery(summed.values("used"))
