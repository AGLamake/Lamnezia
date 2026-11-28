"""Load on a server's address: active keys and traffic in the last 24 hours against thresholds.

Many users or much traffic through one IP is a disputed sign of a VPN to a censor, so both
thresholds are empty (off) until an administrator sets them. The monitor checks every answered
reading. Keys are counted on the server they were issued to (in a server group, the leading
one); traffic where it flowed (TrafficHourly by source). Administrators hear of a high load at
most once per `repeat`, also while it lasts, and of its end once, only when its start was told.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import F, Sum

from apps.core.models import SiteSettings
from apps.keys.models import AccessKey, KeyStatus
from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.protocols.models import ProtocolInstance
from apps.servers.models import AddressAlert, AddressMeasure, Server
from apps.traffic.models import TrafficHourly

GB = 1000**3
_DAY = timedelta(days=1)


@dataclass(frozen=True)
class AddressRules:
    """Thresholds by measure (None: not watched) and the repeat of the alert."""

    thresholds: dict[AddressMeasure, int | None]
    repeat: timedelta

    @classmethod
    def from_site(cls) -> "AddressRules":
        site = SiteSettings.load()
        return cls(
            {AddressMeasure.KEYS: site.address_keys, AddressMeasure.TRAFFIC: site.address_daily_gb},
            timedelta(hours=site.load_repeat_hours),
        )

    @property
    def watched(self) -> bool:
        return any(self.thresholds.values())


class AddressLoad:
    """The measures of one server's address at a moment."""

    def __init__(self, server: Server, now: datetime) -> None:
        self._sources = ProtocolInstance.sources_of(server)
        self._now = now

    def value(self, measure: AddressMeasure) -> int:
        if measure == AddressMeasure.KEYS:
            return self.active_keys()
        return self.daily_gb()

    def active_keys(self) -> int:
        return AccessKey.objects.filter(source__in=self._sources, status=KeyStatus.ACTIVE).count()

    def daily_gb(self) -> int:
        """Whole gigabytes both ways in the last 24 hours, the current hour included."""
        return self.daily_bytes() // GB

    def daily_bytes(self) -> int:
        """Bytes both ways in the last 24 hours, the current hour included: one query."""
        hours = TrafficHourly.objects.filter(
            source__in=self._sources, hour__gt=self._now - _DAY, hour__lte=self._now
        )
        return int(hours.aggregate(total=Sum(F("rx_bytes") + F("tx_bytes")))["total"] or 0)


class AddressWatch:
    """Keeps the `AddressAlert` rows of a server in step with its address load."""

    def __init__(self, rules: AddressRules) -> None:
        self._rules = rules

    def check(self, server: Server, now: datetime) -> None:
        alerts = {alert.measure: alert for alert in server.address_alerts.all()}
        if not self._rules.watched and not any(a.high_since for a in alerts.values()):
            return
        load = AddressLoad(server, now)
        for measure in AddressMeasure:
            alert = alerts.get(measure) or AddressAlert(server=server, measure=measure)
            self._check(alert, load, now)

    def _check(self, alert: AddressAlert, load: AddressLoad, now: datetime) -> None:
        threshold = self._rules.thresholds[AddressMeasure(alert.measure)]
        if not threshold:
            if alert.high_since is not None:
                alert.high_since = None
                alert.save()
            return
        alert.value = load.value(AddressMeasure(alert.measure))
        if alert.value >= threshold:
            self._high(alert, threshold, now)
        elif alert.high_since is not None:
            self._normal(alert, threshold)
        elif alert.pk is not None:
            alert.save(update_fields=["value"])

    def _high(self, alert: AddressAlert, threshold: int, now: datetime) -> None:
        if alert.high_since is None:
            alert.high_since = now
        if alert.told_at is None or now - alert.told_at >= self._rules.repeat:
            alert.told_at = now
            emit(
                Event.ADDRESS_BUSY,
                _data(alert, threshold),
                key=f"{Event.ADDRESS_BUSY}:{alert.server_id}:{alert.measure}:{now.isoformat()}",
            )
        alert.save()

    def _normal(self, alert: AddressAlert, threshold: int) -> None:
        since = alert.high_since
        assert since is not None
        if alert.told_at is not None and alert.told_at >= since:
            emit(
                Event.ADDRESS_CALM,
                _data(alert, threshold),
                key=f"{Event.ADDRESS_CALM}:{alert.server_id}:{alert.measure}:{since.isoformat()}",
            )
        alert.high_since = None
        alert.save()


def _data(alert: AddressAlert, threshold: int) -> dict[str, object]:
    server = alert.server
    since = alert.high_since
    return {
        "server": server.name,
        "host": server.host,
        "measure": alert.measure,
        "value": alert.value,
        "threshold": threshold,
        "since": since.isoformat() if since else "",
    }
