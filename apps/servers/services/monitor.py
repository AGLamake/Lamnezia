"""Server monitoring: a reading of every server per beat cycle and a week of history."""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.db.models import Avg, Count, ExpressionWrapper, F, FloatField, Max, Min, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.servers.models import Server, ServerSample
from apps.servers.services.connection import connect
from apps.servers.services.metrics import METRICS_SCRIPT, CpuTimes, HostMetrics, parse_metrics
from apps.ssh.errors import SshError
from apps.ssh.services import RemoteHost

logger = logging.getLogger(__name__)

Connect = Callable[[Server], AbstractContextManager[RemoteHost]]


class ServerMonitor:
    """Reads CPU, memory, disk, uptime and containers; a server that does not answer is recorded."""

    def __init__(
        self,
        history: timedelta,
        connect: Connect = connect,
        clock: Callable[[], datetime] = timezone.now,
    ) -> None:
        self._history = history
        self._connect = connect
        self._clock = clock

    def prune(self) -> None:
        """Readings older than the history kept are deleted."""
        ServerSample.objects.filter(at__lt=self._clock() - self._history).delete()

    def poll(self, server: Server) -> ServerSample:
        try:
            with self._connect(server) as host:
                output = host.run(METRICS_SCRIPT)
        except SshError as error:
            logger.warning("server %s did not answer: %s", server.name, error.code)
            sample = ServerSample(server=server, at=self._clock(), error=error.code)
        else:
            sample = _sample(server, parse_metrics(output), self._clock())
        sample.save()
        server.record_sample(sample)
        if server.online is False:
            _report_down(server)
        return sample


def _report_down(server: Server) -> None:
    """Once per outage: the key holds the last answer, which changes only after a new one."""
    last_seen = server.last_seen_at.isoformat() if server.last_seen_at else ""
    emit(
        Event.SERVER_DOWN,
        {"server": server.name, "host": server.host, "last_seen": last_seen},
        key=f"server_down:{server.pk}:{last_seen or 'never'}",
    )


def _sample(server: Server, metrics: HostMetrics, at: datetime) -> ServerSample:
    cpu = metrics.cpu
    return ServerSample(
        server=server,
        at=at,
        cpu_percent=_cpu_percent(server.latest_sample, cpu),
        cpu_total_ticks=cpu.total if cpu else None,
        cpu_idle_ticks=cpu.idle if cpu else None,
        memory_total=metrics.memory_total,
        memory_available=metrics.memory_available,
        disk_total=metrics.disk_total,
        disk_free=metrics.disk_free,
        uptime=metrics.uptime,
        containers=[{"name": c.name, "state": c.state} for c in metrics.containers],
    )


def _cpu_percent(previous: ServerSample | None, cpu: CpuTimes | None) -> float | None:
    if previous is None or cpu is None:
        return None
    if previous.cpu_total_ticks is None or previous.cpu_idle_ticks is None:
        return None
    return cpu.busy_percent_since(CpuTimes(previous.cpu_total_ticks, previous.cpu_idle_ticks))


@dataclass(frozen=True)
class DayHealth:
    """A day of readings: share of answered ones, average CPU, peak memory, least free disk."""

    day: date
    availability: float
    cpu: float | None
    memory: float | None
    disk_free: int | None


def daily_history(server: Server, days: int) -> list[DayHealth]:
    """The last `days` days that have readings, newest first."""
    memory_percent = ExpressionWrapper(
        100.0 * (F("memory_total") - F("memory_available")) / F("memory_total"),
        output_field=FloatField(),
    )
    rows = (
        server.samples.filter(at__gte=timezone.now() - timedelta(days=days))
        .annotate(day=TruncDate("at"))
        .values("day")
        .annotate(
            readings=Count("id"),
            replies=Count("id", filter=Q(error="")),
            cpu=Avg("cpu_percent"),
            memory=Max(memory_percent),
            disk_free=Min("disk_free"),
        )
        .order_by("-day")
    )
    return [
        DayHealth(
            row["day"],
            round(100 * row["replies"] / row["readings"], 1),
            row["cpu"],
            row["memory"],
            row["disk_free"],
        )
        for row in rows
    ]
