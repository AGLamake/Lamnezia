"""Telling users ahead that their access term ends (`access_expiring`) or their traffic runs out
(`traffic_low`).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Self

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import User
from apps.core.services.site_settings import cached_settings
from apps.notifications.events import Event, english
from apps.notifications.services import delivery
from apps.plans.models import UserLimits
from apps.plans.services.limits import PLAN_SERVERS, EffectiveLimits
from apps.traffic.services.limits import LimitEnforcer
from apps.traffic.services.unknown import users_with_unknown_traffic

# Which limit a traffic warning is about, as its `limit` detail reads.
_LIMITS = {"period": _("of the period"), "total": _("of the plan")}
_UNCOUNTED = _("Traffic of some of your keys is not counted now: the real share may be higher.")


def warn_expiring(now: datetime, notice: timedelta) -> None:
    """Once per term: the key holds the end, so an extension that moves it warns again."""
    subscriptions = UserLimits.objects.filter(plan__isnull=False, user__is_active=True)
    for limits in subscriptions.select_related("plan", "user"):
        until = EffectiveLimits.of(limits).access_until
        if until is None or not now < until <= now + notice:
            continue
        user = limits.user
        delivery.emit(
            Event.ACCESS_EXPIRING,
            {"user": user.username, "until": timezone.localtime(until).date().isoformat()},
            key=f"access_expiring:{user.pk}:{until.isoformat()}",
            user=user,
        )


@dataclass(frozen=True)
class _Allowance:
    """One traffic limit of a user and the bytes counted toward it since `since`."""

    kind: str
    limit: int
    used: int
    since: datetime | None

    @classmethod
    def of(cls, limits: EffectiveLimits, now: datetime, usage: tuple[int, int]) -> list[Self]:
        """The traffic limits the user has: of the current period and of the whole plan."""
        in_period, in_total = usage
        found = []
        if limits.traffic_limit:
            found.append(
                cls("period", limits.traffic_limit, in_period, limits.current_period_start(now))
            )
        if limits.total_traffic_limit:
            found.append(cls("total", limits.total_traffic_limit, in_total, limits.period_start))
        return found

    @property
    def percent(self) -> int:
        return self.used * 100 // self.limit

    @property
    def reached(self) -> bool:
        return self.used >= self.limit


class TrafficWarnings:
    """Users who have used `percent` of a traffic limit hear of it once per period of that limit.

    The occurrence is keyed by the start of the period and the limit itself: a new period, a new
    plan or a changed limit warns again. A reached limit is not warned of: `limit_reached` tells
    the user. Traffic is counted as the limits count it, the readings of group servers included.
    """

    def __init__(self, now: datetime, percent: int) -> None:
        self._now = now
        self._percent = percent

    @classmethod
    def from_settings(cls, now: datetime) -> Self:
        return cls(now, cached_settings.get().traffic_warning_percent)

    def warn_all(self) -> None:
        if not self._percent:
            return
        subscribers = self._subscribers()
        counted = {user.pk: limits for user, limits in subscribers}
        usage = LimitEnforcer(self._now).usages(counted)
        uncounted = users_with_unknown_traffic(counted)
        for user, limits in subscribers:
            allowances = _Allowance.of(limits, self._now, usage.get(user.pk, (0, 0)))
            if any(allowance.reached for allowance in allowances):
                continue
            for allowance in allowances:
                if allowance.percent >= self._percent:
                    self._warn(user, allowance, uncounted=user.pk in uncounted)

    def _subscribers(self) -> list[tuple[User, EffectiveLimits]]:
        """Active users with a traffic limit and an access term not yet over."""
        rows = (
            UserLimits.objects.filter(plan__isnull=False, user__is_active=True)
            .select_related("plan", "user")
            .prefetch_related(*PLAN_SERVERS)
        )
        found = [(row.user, EffectiveLimits.of(row)) for row in rows]
        return [
            (user, limits)
            for user, limits in found
            if (limits.traffic_limit or limits.total_traffic_limit)
            and not limits.is_expired(self._now)
        ]

    def _warn(self, user: User, allowance: _Allowance, *, uncounted: bool) -> None:
        since = allowance.since.isoformat() if allowance.since else "ever"
        delivery.emit(
            Event.TRAFFIC_LOW,
            {
                "user": user.username,
                "percent": allowance.percent,
                "used": allowance.used,
                "limit_bytes": allowance.limit,
                "limit": english(_LIMITS[allowance.kind]),
                "incomplete": uncounted,
                "note": english(_UNCOUNTED) if uncounted else "",
                "url": settings.SITE_URL.rstrip("/") + reverse("portal:usage"),
            },
            key=f"traffic_low:{user.pk}:{allowance.kind}:{since}:{allowance.limit}",
            user=user,
        )
