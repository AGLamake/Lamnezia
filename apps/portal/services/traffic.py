"""The user's own traffic in the portal: by key over a day, week or month, and when the limit of
the period runs out at the current pace.

Only the user's keys are ever counted: a key of someone else asked for in the address is ignored.
"""

from dataclasses import dataclass
from datetime import datetime

from django.utils.functional import cached_property

from apps.accounts.models import User
from apps.dashboard.services.forecast import Exhaustion, LimitForecast
from apps.dashboard.services.stats import KeyTraffic, Period, Point, Scope, TrafficStats, Window
from apps.keys.models import GONE_STATUSES, AccessKey
from apps.traffic.services.online import OnlineRule


@dataclass(frozen=True)
class MyTraffic:
    """Traffic of `user` in the window of `period`, of one `key` of theirs or of all of them."""

    user: User
    period: Period
    now: datetime
    key: AccessKey | None = None

    @classmethod
    def of(cls, user: User, period: str, key_id: str, now: datetime) -> "MyTraffic":
        """From the page address; an unknown period or a key that is not the user's is ignored."""
        chosen = Period(period) if period in Period.values else Period.WEEK
        key = user.access_keys.filter(pk=int(key_id)).first() if key_id.isdigit() else None
        return cls(user, chosen, now, key)

    @property
    def window(self) -> Window:
        return Window(self.period, self.now)

    @cached_property
    def _stats(self) -> TrafficStats:
        scope = Scope(user=self.user, key=self.key)
        return TrafficStats(scope, self.window, OnlineRule.from_settings())

    @cached_property
    def series(self) -> list[Point]:
        return self._stats.series()

    @property
    def downloaded(self) -> int:
        return sum(point.tx for point in self.series)

    @property
    def uploaded(self) -> int:
        return sum(point.rx for point in self.series)

    @cached_property
    def keys(self) -> list[KeyTraffic]:
        """The user's keys, the busiest first; a deleted one only while it has traffic here."""
        return [
            row for row in self._stats.by_key() if row.total or row.key.status not in GONE_STATUSES
        ]

    @property
    def busiest(self) -> int:
        """Traffic of the busiest key, the full width of the bars."""
        return max((row.total for row in self.keys), default=0)

    def forecast(self) -> Exhaustion | None:
        """When the traffic of the period runs out at this pace, if before the period ends."""
        found = LimitForecast(self.now).soonest(User.objects.filter(pk=self.user.pk))
        return found[0] if found else None
