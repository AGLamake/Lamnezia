"""The usage of a user: the portal bar (traffic against the plan, reset date, access term) and the
same for XRay clients in the `subscription-userinfo` header of the subscription link.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Self

from apps.accounts.models import User
from apps.plans.services.limits import EffectiveLimits
from apps.traffic.services.limits import LimitEnforcer, traffic_by_direction

# Share of a limit after which the user is warned.
_WARN_SHARE = 0.8
# The access term ending sooner than this is a warning.
_WARN_BEFORE_END = timedelta(days=7)


@dataclass(frozen=True)
class Usage:
    """Traffic is in bytes; `None` in a limit means unlimited."""

    period_used: int
    period_limit: int | None
    resets_at: datetime | None
    total_used: int
    total_limit: int | None
    access_until: datetime | None
    now: datetime

    @classmethod
    def of(cls, user: User, limits: EffectiveLimits, now: datetime) -> Self:
        period, total = LimitEnforcer(now).usage(user, limits)
        start = limits.current_period_start(now)
        resets_at = start + timedelta(days=limits.period_days) if start else None
        return cls(
            period,
            limits.traffic_limit,
            resets_at if limits.traffic_limit is not None else None,
            total,
            limits.total_traffic_limit,
            limits.access_until,
            now,
        )

    @property
    def period_percent(self) -> int | None:
        return _percent(self.period_used, self.period_limit)

    @property
    def traffic_low(self) -> bool:
        """Some traffic limit is reached or close."""
        return any(
            limit is not None and used >= limit * _WARN_SHARE
            for used, limit in (
                (self.period_used, self.period_limit),
                (self.total_used, self.total_limit),
            )
        )

    @property
    def expired(self) -> bool:
        return self.access_until is not None and self.now >= self.access_until

    @property
    def days_left(self) -> int | None:
        """Whole days until the access ends, counting a started day; 0 once it has ended."""
        if self.access_until is None:
            return None
        left = self.access_until - self.now
        return max(0, left.days + (1 if left.seconds or left.microseconds else 0))

    @property
    def expires_soon(self) -> bool:
        return (
            self.access_until is not None
            and not self.expired
            and self.access_until - self.now <= _WARN_BEFORE_END
        )


@dataclass(frozen=True)
class SubscriptionUserinfo:
    """Traffic and term for the `subscription-userinfo` header XRay clients show.

    The header holds one counter, so it is the limit with less left: the period one counts from
    the period start, the total one from the plan assignment. Without a traffic limit `total` is 0,
    which the clients read as unlimited, and the current period is counted. `expire` 0 is no end.
    """

    upload: int
    download: int
    total: int
    expire: int

    @classmethod
    def of(cls, user: User, limits: EffectiveLimits, now: datetime) -> Self:
        counters = [
            (start, limit)
            for start, limit in (
                (limits.current_period_start(now), limits.traffic_limit),
                (limits.period_start, limits.total_traffic_limit),
            )
            if limit is not None
        ] or [(limits.current_period_start(now), 0)]
        upload, download, total = min(
            ((*traffic_by_direction(user, start), limit) for start, limit in counters),
            key=lambda counter: counter[2] - counter[0] - counter[1],
        )
        expire = int(limits.access_until.timestamp()) if limits.access_until else 0
        return cls(upload, download, total, expire)

    def header(self) -> str:
        return (
            f"upload={self.upload}; download={self.download}; "
            f"total={self.total}; expire={self.expire}"
        )


def _percent(used: int, limit: int | None) -> int | None:
    if limit is None:
        return None
    if limit == 0:
        return 100
    return min(100, used * 100 // limit)
