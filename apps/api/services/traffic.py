"""Traffic through the API: per hour or day over a period and per key, within the token's reach."""

from datetime import datetime
from typing import Any

from django.db.models import Model, QuerySet
from django.utils import timezone

from apps.accounts.services.zone import Zone
from apps.api.errors import ForbiddenError, NotFoundError
from apps.api.services.permissions import Reach
from apps.dashboard.services.stats import Period, Scope, TrafficStats, Window
from apps.keys.services.zone import KeyZone
from apps.traffic.services.online import OnlineRule


class TrafficReport:
    """The staff statistics of the scope (`apps.dashboard.services.stats`) as an API answer.

    A token whose traffic permissions are bound to objects names one of them in the call;
    the other filters only narrow it further.
    """

    def __init__(
        self, zone: Zone, key_zone: KeyZone, reach: Reach, now: datetime | None = None
    ) -> None:
        self._servers = zone.servers()
        self._zone = key_zone
        self._reach = reach
        self._now = now or timezone.now()

    def answer(
        self,
        period: Period,
        *,
        server: int | None = None,
        user: int | None = None,
        key: int | None = None,
    ) -> dict[str, Any]:
        found_server = _one(self._servers, server)
        found_user = _one(self._zone.users(), user)
        found_key = _one(self._zone.keys(), key)
        if not self._reach.whole:
            covered = (
                (found_server is not None and self._reach.has_server(found_server.pk))
                or (found_user is not None and self._reach.has_user(found_user.pk))
                or (found_key is not None and self._reach.has_key(found_key))
            )
            if not covered and (server or user or key):
                raise NotFoundError
            if not covered:
                raise ForbiddenError("Name a server, user or key the token may read.")
        scope = Scope(server=found_server, user=found_user, key=found_key, zone=self._zone)
        return self._answer(period, scope)

    def _answer(self, period: Period, scope: Scope) -> dict[str, Any]:
        window = Window(period, self._now)
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


def _one[M: Model](rows: QuerySet[M], pk: int | None) -> M | None:
    """The object of the zone with this id; None when no id is given, 404 when it is not there."""
    if pk is None:
        return None
    found = rows.filter(pk=pk).first()
    if found is None:
        raise NotFoundError
    return found
