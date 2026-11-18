"""Traffic through the API: per hour or day over a period and per key, within the token's zone."""

from datetime import datetime
from typing import Any

from django.utils import timezone

from apps.accounts.models import User
from apps.dashboard.services.stats import Period, Scope, TrafficStats, Window
from apps.keys.models import AccessKey
from apps.keys.services.zone import KeyZone
from apps.servers.models import Server
from apps.traffic.services.online import OnlineRule


class TrafficReport:
    """The staff statistics of the scope (`apps.dashboard.services.stats`) as an API answer."""

    def __init__(self, zone: KeyZone, now: datetime | None = None) -> None:
        self._zone = zone
        self._now = now or timezone.now()

    def answer(
        self,
        period: Period,
        *,
        server: Server | None = None,
        user: User | None = None,
        key: AccessKey | None = None,
    ) -> dict[str, Any]:
        window = Window(period, self._now)
        scope = Scope(server=server, user=user, key=key, zone=self._zone)
        stats = TrafficStats(scope, window, OnlineRule.from_settings())
        return {
            "period": period.value,
            "bucket": "hour" if window.hourly else "day",
            "since": window.start,
            "series": [
                {"at": point.at, "rx_bytes": point.rx, "tx_bytes": point.tx}
                for point in stats.series()
            ],
            "keys": [
                {
                    "key_id": row.key.pk,
                    "rx_bytes": row.rx,
                    "tx_bytes": row.tx,
                    "known": row.has_data,
                }
                for row in stats.by_key()
            ],
        }
