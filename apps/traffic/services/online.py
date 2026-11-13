"""Which peers are online (DESIGN.md 5.5): one rule for the statistics page and the metrics."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Q


@dataclass(frozen=True)
class OnlineRule:
    """A peer on the server is online with a recent handshake or, for XRay that has no
    handshake, with counters that grew in a recent reading.

    Growth is seen only at a reading, so its window is one collection interval longer than the
    handshake one: an XRay key in use stays online between two readings.
    """

    handshake_window: timedelta
    activity_window: timedelta

    @classmethod
    def from_settings(cls) -> "OnlineRule":
        window = timedelta(seconds=settings.TRAFFIC_ONLINE_WINDOW)
        return cls(window, window + timedelta(seconds=settings.TRAFFIC_INTERVAL))

    def filter(self, now: datetime) -> Q:
        """Condition on `TrafficCounter` rows that are online at `now`."""
        recent = Q(latest_handshake__gt=now - self.handshake_window) | Q(
            active_at__gt=now - self.activity_window
        )
        return Q(present=True) & recent
