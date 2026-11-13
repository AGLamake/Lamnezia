"""Key numbers of the staff dashboard: servers up, keys, users, online peers and recent traffic.

A moderator sees the numbers of their own clients and servers (`Zone`), with the clients whose
access ends soon in place of the servers online.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cached_property

from django.db.models import QuerySet
from django.utils import timezone

from apps.accounts.models import Role, Status, User
from apps.accounts.services.zone import Zone
from apps.dashboard.services.stats import Period, Point, Scope, TrafficStats, Window
from apps.dashboard.services.users import UserRow, users_for_list
from apps.keys.models import KeyStatus
from apps.keys.services.zone import KeyZone
from apps.servers.models import Server
from apps.traffic.services.online import OnlineRule

# A client whose access ends within this many days is worth a reminder.
ACCESS_ENDING_DAYS = 7


@dataclass(frozen=True)
class ServerCount:
    online: int
    total: int


class Overview:
    """The numbers at the top of the dashboard, within the member's zone."""

    def __init__(self, member: User, servers: list[Server], now: datetime | None = None) -> None:
        self._member = member
        self._servers = servers
        self._now = now or timezone.now()
        self._stats = TrafficStats(
            Scope(zone=KeyZone(member)),
            Window(Period.WEEK, self._now),
            OnlineRule.from_settings(),
        )

    def servers(self) -> ServerCount:
        return ServerCount(sum(1 for s in self._servers if s.online), len(self._servers))

    def active_keys(self) -> int:
        return Scope(zone=KeyZone(self._member)).keys().filter(status=KeyStatus.ACTIVE).count()

    def users(self) -> int:
        return self._clients().count()

    def access_ending(self) -> int:
        """Clients whose access ends within `ACCESS_ENDING_DAYS`: a moderator's next calls."""
        soon = self._now + timedelta(days=ACCESS_ENDING_DAYS)
        rows = UserRow.rows(users_for_list().filter(pk__in=self._clients()))
        return sum(1 for row in rows if row.access_until and self._now <= row.access_until <= soon)

    def _clients(self) -> QuerySet[User]:
        return Zone(self._member).users().filter(status=Status.ACTIVE, role=Role.USER)

    def online(self) -> int:
        return len(self._stats.online())

    @cached_property
    def week(self) -> list[Point]:
        """Traffic of the last seven days, today last."""
        return self._stats.series()

    def today(self) -> int:
        return self.week[-1].rx + self.week[-1].tx if self.week else 0

    def week_total(self) -> int:
        return sum(point.rx + point.tx for point in self.week)
