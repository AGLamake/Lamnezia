"""When users run out of their traffic per period if they keep their pace (DESIGN.md 6.1):
the traffic of the period so far, spread over the time it took, is carried on to the limit.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import QuerySet

from apps.accounts.models import User
from apps.plans.models import UserLimits
from apps.plans.services.limits import EffectiveLimits
from apps.traffic.services.limits import LimitEnforcer

_SIZE = 20


@dataclass(frozen=True)
class Exhaustion:
    """`at` is when the limit runs out; for a limit already reached it is `now`."""

    user: User
    used: int
    limit: int
    at: datetime

    @property
    def reached(self) -> bool:
        return self.used >= self.limit


@dataclass(frozen=True)
class _Subscription:
    user: User
    limits: EffectiveLimits
    limit: int
    period_start: datetime
    period_end: datetime


class LimitForecast:
    """Users of the scope who reach their traffic limit before their period ends."""

    def __init__(self, now: datetime, size: int = _SIZE) -> None:
        self._now = now
        self._size = size

    def soonest(self, users: QuerySet[User]) -> list[Exhaustion]:
        subscriptions = self._subscriptions(users)
        if not subscriptions:
            return []
        used = self._used(subscriptions)
        forecasts = [
            forecast
            for subscription in subscriptions
            if (forecast := self._forecast(subscription, used.get(subscription.user.pk, 0)))
        ]
        return sorted(forecasts, key=lambda forecast: forecast.at)[: self._size]

    def _subscriptions(self, users: QuerySet[User]) -> list[_Subscription]:
        rows = (
            UserLimits.objects.filter(user__in=users, plan__isnull=False)
            .select_related("plan", "user")
            .prefetch_related("plan__servers")
        )
        subscriptions = []
        for limits in rows:
            effective = EffectiveLimits.of(limits)
            start = effective.current_period_start(self._now)
            if effective.traffic_limit is None or start is None or effective.is_expired(self._now):
                continue
            end = start + timedelta(days=effective.period_days)
            subscriptions.append(
                _Subscription(limits.user, effective, effective.traffic_limit, start, end)
            )
        return subscriptions

    def _used(self, subscriptions: list[_Subscription]) -> dict[int, int]:
        """Traffic of each user since their period started, as `LimitEnforcer` counts it."""
        limits = {item.user.pk: item.limits for item in subscriptions}
        usages = LimitEnforcer(self._now).usages(limits)
        return {user_id: period for user_id, (period, _total) in usages.items()}

    def _forecast(self, subscription: _Subscription, used: int) -> Exhaustion | None:
        if used >= subscription.limit:
            return Exhaustion(subscription.user, used, subscription.limit, self._now)
        elapsed = (self._now - subscription.period_start).total_seconds()
        if used == 0 or elapsed <= 0:
            return None
        left = (subscription.limit - used) * elapsed / used
        # Compared in seconds first: a big limit at a slow pace is beyond what a date holds.
        if left >= (subscription.period_end - self._now).total_seconds():
            return None
        at = self._now + timedelta(seconds=left)
        return Exhaustion(subscription.user, used, subscription.limit, at)
