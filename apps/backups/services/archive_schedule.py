"""Panel archives on schedule: due by the newest archive, made with the password of the settings.

The password lives only in the .env of the panel (LAMNEZIA_PANEL_ARCHIVE_PASSWORD): the database
and the archive are both inside the archive, and so is the key the panel encrypts secrets with.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings

from apps.backups.errors import PanelArchiveError, PanelArchiveErrorCode
from apps.backups.services.archive_cipher import MIN_PASSWORD_LENGTH
from apps.backups.services.panel_archives import PanelArchiveFolder, start_scheduled_archive
from apps.operations.models import Operation

# Beat looks hourly: an archive this much short of the interval is due already, or a daily one
# would slip by an hour every day.
_EARLY = timedelta(minutes=30)


@dataclass(frozen=True)
class ArchiveSchedule:
    """On while the password is set; any archive, made by hand too, counts as the last one."""

    password: str
    interval: timedelta

    @classmethod
    def from_settings(cls) -> "ArchiveSchedule":
        return cls(
            settings.PANEL_ARCHIVE_PASSWORD, timedelta(hours=settings.PANEL_ARCHIVE_INTERVAL)
        )

    @property
    def enabled(self) -> bool:
        return bool(self.password) and self.interval > timedelta(0)

    @property
    def interval_hours(self) -> int:
        return int(self.interval.total_seconds()) // 3600

    def checked_password(self) -> str:
        if len(self.password) < MIN_PASSWORD_LENGTH:
            raise PanelArchiveError(PanelArchiveErrorCode.SHORT_PASSWORD)
        return self.password

    def due(self, newest: datetime | None, now: datetime) -> bool:
        return self.enabled and (newest is None or now - newest >= self.interval - _EARLY)

    def start_due(self, folder: PanelArchiveFolder, now: datetime) -> Operation | None:
        """Queue the archive due now; None when none is due."""
        archives = folder.archives()
        newest = archives[0].created_at if archives else None
        return start_scheduled_archive() if self.due(newest, now) else None
