"""Load alerts: CPU, memory or disk of a server above the threshold for several readings in a row.

The monitor checks every answered reading. A resource is high after `readings` answered readings
in a row at or above its threshold and normal again after as many below it, so a load swinging
around the threshold changes nothing. Administrators hear of a high load at most once per
`repeat`, also while it lasts, and of its end once, only when its start was told.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from apps.core.models import SiteSettings
from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.servers.models import LoadAlert, LoadResource, Server, ServerSample


@dataclass(frozen=True)
class LoadRules:
    """Thresholds in percent by resource (0: not watched), the run length and the repeat."""

    thresholds: dict[LoadResource, int]
    readings: int
    repeat: timedelta

    @classmethod
    def from_site(cls) -> "LoadRules":
        site = SiteSettings.load()
        return cls(
            {
                LoadResource.CPU: site.load_cpu_percent,
                LoadResource.MEMORY: site.load_memory_percent,
                LoadResource.DISK: site.load_disk_percent,
            },
            site.load_readings,
            timedelta(hours=site.load_repeat_hours),
        )


class LoadWatch:
    """Keeps the `LoadAlert` rows of a server in step with its latest readings."""

    def __init__(self, rules: LoadRules) -> None:
        self._rules = rules

    def check(self, server: Server, now: datetime) -> None:
        recent = list(
            server.samples.filter(error="", at__lte=now).order_by("-at")[: self._rules.readings]
        )
        alerts = {alert.resource: alert for alert in server.load_alerts.all()}
        for resource in LoadResource:
            alert = alerts.get(resource) or LoadAlert(server=server, resource=resource)
            self._check(alert, recent, now)

    def _check(self, alert: LoadAlert, recent: Sequence[ServerSample], now: datetime) -> None:
        threshold = self._rules.thresholds[LoadResource(alert.resource)]
        if not threshold:
            if alert.high_since is not None:
                alert.high_since = None
                alert.save()
            return
        values = [sample.used_percent(LoadResource(alert.resource)) for sample in recent]
        known = [value for value in values if value is not None]
        if len(known) < self._rules.readings:
            return
        if all(value >= threshold for value in known):
            self._high(alert, recent[-1].at, known[0], threshold, now)
        elif alert.high_since is not None and all(value < threshold for value in known):
            self._normal(alert, known[0], threshold)

    def _high(
        self, alert: LoadAlert, since: datetime, percent: float, threshold: int, now: datetime
    ) -> None:
        if alert.high_since is None:
            alert.high_since = since
        if alert.told_at is None or now - alert.told_at >= self._rules.repeat:
            alert.told_at = now
            emit(
                Event.SERVER_LOAD_HIGH,
                _data(alert, percent, threshold),
                key=f"{Event.SERVER_LOAD_HIGH}:{alert.server_id}:{alert.resource}:{now.isoformat()}",
            )
        alert.save()

    def _normal(self, alert: LoadAlert, percent: float, threshold: int) -> None:
        since = alert.high_since
        assert since is not None
        if alert.told_at is not None and alert.told_at >= since:
            emit(
                Event.SERVER_LOAD_NORMAL,
                _data(alert, percent, threshold),
                key=f"{Event.SERVER_LOAD_NORMAL}:{alert.server_id}:{alert.resource}:"
                f"{since.isoformat()}",
            )
        alert.high_since = None
        alert.save()


def _data(alert: LoadAlert, percent: float, threshold: int) -> dict[str, object]:
    server = alert.server
    since = alert.high_since
    return {
        "server": server.name,
        "host": server.host,
        "resource": alert.resource,
        "percent": round(percent),
        "threshold": threshold,
        "since": since.isoformat() if since else "",
    }
