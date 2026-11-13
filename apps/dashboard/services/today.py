"""Traffic since local midnight, of the whole panel and of each server, for the staff bots."""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import F, QuerySet, Sum
from django.utils import timezone

from apps.dashboard.services.stats import Scope
from apps.servers.models import Server
from apps.traffic.models import TrafficHourly


@dataclass(frozen=True)
class ServerTraffic:
    server: Server
    total: int


@dataclass(frozen=True)
class TodayTraffic:
    total: int
    servers: list[ServerTraffic]


def traffic_today(
    now: datetime | None = None, servers: QuerySet[Server] | None = None
) -> TodayTraffic:
    """Bytes both ways since midnight in the panel's time zone; servers with traffic, most first.

    Given `servers`, only theirs is counted, the total too.
    """
    midnight = timezone.localtime(now or timezone.now()).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    rows = TrafficHourly.objects.filter(hour__gte=midnight)
    counted = [
        ServerTraffic(server, _total(Scope(server=server).traffic(rows)))
        for server in (Server.objects.all() if servers is None else servers)
    ]
    total = _total(rows) if servers is None else sum(s.total for s in counted)
    return TodayTraffic(
        total, sorted((s for s in counted if s.total), key=lambda s: s.total, reverse=True)
    )


def _total(rows: QuerySet[TrafficHourly]) -> int:
    total: int | None = rows.aggregate(total=Sum(F("rx_bytes") + F("tx_bytes")))["total"]
    return total or 0
