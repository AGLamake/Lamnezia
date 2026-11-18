"""The scheduled report to the staff: what is down, traffic, access ending, failures, backups and
what waits, built from the dashboard's own numbers with no request to the servers.

The channels routed for `staff_report` get the whole panel; each moderator gets their own zone
(`zone_report`) in their messenger or by email. A report is named by its day or week, so it goes
once however often the beat asks. Nothing in it names a resource a user visited.
"""

from datetime import datetime
from typing import Any

from django.conf import settings
from django.db.models import Min
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, Status, User
from apps.accounts.services.applications import Applications
from apps.accounts.services.zone import Zone
from apps.backups.models import ProtocolBackup
from apps.backups.services.panel_archives import PanelArchiveFolder
from apps.core.models import ReportFrequency
from apps.core.services.site_settings import cached_settings
from apps.dashboard.services.attention import Attention
from apps.dashboard.services.overview import Overview
from apps.dashboard.services.stats import Period, Scope, TrafficStats, Window
from apps.keys.services.zone import KeyZone
from apps.notifications.events import Event
from apps.notifications.models import Channel, Notification
from apps.notifications.services.delivery import emit
from apps.support.models import Kind
from apps.support.services.inbox import Inbox
from apps.traffic.services.online import OnlineRule

# The report of the day goes from this local hour on; the weekly one on Monday.
REPORT_HOUR = 9
TOP_KEYS = 5

_PERIODS = {ReportFrequency.DAILY: Period.DAY, ReportFrequency.WEEKLY: Period.WEEK}


class StaffReport:
    """The details of one report as `member` sees the panel: a moderator only their zone."""

    def __init__(self, member: User, frequency: ReportFrequency, now: datetime) -> None:
        self._member = member
        self._frequency = frequency
        self._window = Window(_PERIODS[frequency], now)
        self._attention = Attention(member, now)

    def data(self) -> dict[str, Any]:
        start = self._window.start
        stats = TrafficStats(
            Scope(zone=KeyZone(self._member)), self._window, OnlineRule.from_settings()
        )
        top = [row for row in stats.by_key()[:TOP_KEYS] if row.total]
        overview = Overview(self._member, self._attention.servers, self._window.now)
        data: dict[str, Any] = {
            "period": self._frequency.value,
            "since": _stamp(start),
            "servers_down": [server.name for server in self._attention.offline()],
            "traffic": sum(point.rx + point.tx for point in stats.series()),
            "top_keys": [[row.key.name, row.total] for row in top],
            "access_ending": [
                f"{row.user.username} {timezone.localdate(row.access_until).isoformat()}"
                for row in overview.access_ending_rows()
                if row.access_until
            ],
            "failed_operations": self._attention.failed_since(start),
            "backups_oldest": self._oldest_backup(),
            "new_requests": Inbox(self._member)
            .visible()
            .filter(kind=Kind.TICKET, created_at__gte=start)
            .count(),
            "url": settings.SITE_URL.rstrip("/") + reverse("dashboard:index"),
        }
        if self._member.is_admin:
            data |= self._administrators()
        return data

    def _administrators(self) -> dict[str, Any]:
        """What only an administrator handles: relays, groups, the panel archive, applications."""
        relays = self._attention.relays
        archives = PanelArchiveFolder.from_settings().archives()
        return {
            "relays_down": [
                relay.server.name
                for relay in relays
                if relay.read_error or relay.server.online is False
            ],
            "groups_down": [group.name for group in self._attention.groups_down()],
            "archive_at": _stamp(archives[0].created_at) if archives else None,
            "applications": Applications.waiting().count(),
        }

    def _oldest_backup(self) -> str | None:
        """The oldest copy of a protocol still installed: every other one is newer."""
        backups = ProtocolBackup.objects.filter(instance__container_removed_at__isnull=True)
        oldest = Zone(self._member).narrow(backups, server="instance__server")
        taken_at = oldest.aggregate(oldest=Min("taken_at"))["oldest"]
        return _stamp(taken_at) if taken_at else None


class StaffReports:
    """Sends the reports: on schedule by `SiteSettings.report_frequency`, or now for a check."""

    def __init__(self, frequency: str, now: datetime) -> None:
        self._frequency = ReportFrequency(frequency)
        self._now = now

    @classmethod
    def from_settings(cls, now: datetime) -> "StaffReports":
        return cls(cached_settings.get().report_frequency, now)

    def due(self) -> str | None:
        """The name of the report due now: its day or ISO week; None when reports are off."""
        local = timezone.localtime(self._now)
        if self._frequency == ReportFrequency.OFF or local.hour < REPORT_HOUR:
            return None
        if self._frequency == ReportFrequency.DAILY:
            return local.date().isoformat()
        year, week, _day = local.isocalendar()
        return f"{year}-W{week:02d}"

    def send_due(self) -> int:
        """Send the report due now to whoever has not had it; the number of reports queued."""
        stamp = self.due()
        return self._send(self._frequency, stamp) if stamp else 0

    def send_now(self) -> int:
        """Send the report at once, a daily one while reports are off; nothing is remembered."""
        frequency = self._frequency
        if frequency == ReportFrequency.OFF:
            frequency = ReportFrequency.DAILY
        return self._send(frequency, None)

    def _send(self, frequency: ReportFrequency, stamp: str | None) -> int:
        sent = 0
        key = f"{Event.STAFF_REPORT}:{stamp}" if stamp else None
        if self._routed() and not self._sent(key):
            data = StaffReport(self._panel(), frequency, self._now).data()
            sent += emit(Event.STAFF_REPORT, data, key=key) is not None
        for moderator in self._moderators():
            key = f"{Event.ZONE_REPORT}:{moderator.pk}:{stamp}" if stamp else None
            if self._sent(key):
                continue
            data = StaffReport(moderator, frequency, self._now).data()
            data["user"] = moderator.username
            sent += emit(Event.ZONE_REPORT, data, key=key, user=moderator) is not None
        return sent

    @staticmethod
    def _panel() -> User:
        """The whole panel, as an administrator sees it: what the channels get."""
        return User(role=Role.ADMIN)

    @staticmethod
    def _routed() -> bool:
        return Channel.objects.filter(enabled=True, routes__event=Event.STAFF_REPORT).exists()

    @staticmethod
    def _sent(key: str | None) -> bool:
        return key is not None and Notification.objects.filter(key=key).exists()

    @staticmethod
    def _moderators() -> list[User]:
        """Moderators the report reaches: a personal notification needs an email."""
        moderators = User.objects.filter(
            role=Role.MODERATOR, status=Status.ACTIVE, is_active=True
        ).exclude(email="")
        return list(moderators.order_by("pk"))


def _stamp(moment: datetime) -> str:
    return timezone.localtime(moment).strftime("%Y-%m-%d %H:%M")
