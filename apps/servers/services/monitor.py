"""Server monitoring: a reading of every server per beat cycle and a week of history."""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from django.db.models import Avg, Count, ExpressionWrapper, F, FloatField, Max, Min, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from amnezia_core.containers import AWG2
from apps.notifications.events import Event
from apps.notifications.services.trouble import (
    forget_trouble,
    open_troubles,
    report_recovery,
    report_trouble,
)
from apps.servers.models import Server, ServerSample
from apps.servers.services.connection import connect
from apps.servers.services.load import LoadRules, LoadWatch
from apps.servers.services.metrics import (
    METRICS_SCRIPT,
    ContainerState,
    CpuTimes,
    HostMetrics,
    parse_metrics,
)
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
        if sample.answered:
            _report_up(server)
            _report_awg3(server, sample)
        server.record_sample(sample)
        if sample.answered:
            LoadWatch(LoadRules.from_site()).check(server, sample.at)
        if server.online is False:
            _report_down(server)
        return sample


def _report_down(server: Server) -> None:
    """Once per outage: the key holds the last answer, which changes only after a new one."""
    last_seen = _last_seen(server)
    report_trouble(
        Event.SERVER_DOWN,
        {"server": server.name, "host": server.host, "last_seen": last_seen},
        key=f"server_down:{server.pk}:{last_seen or 'never'}",
    )


def _report_up(server: Server) -> None:
    """Before an answer is kept: the outage with the same last answer ends."""
    last_seen = _last_seen(server)
    occurrence = f"{server.pk}:{last_seen or 'never'}"
    report_recovery(
        f"server_down:{occurrence}",
        Event.SERVER_UP,
        {"server": server.name, "host": server.host, "last_seen": last_seen},
        key=f"server_up:{occurrence}",
    )


def _report_awg3(server: Server, sample: ServerSample) -> None:
    """Once when amnezia-awg2 drops the 3.1 settings and once when it takes them again.

    The key of a drop names the server and the reading it was first seen in:
    `awg3_dropped:<id>:<moment>`. Without amnezia-awg2 the drop is forgotten untold.
    """
    prefix = f"{Event.AWG3_DROPPED}:{server.pk}:"
    dropped = open_troubles(prefix)
    data = {"server": server.name, "host": server.host}
    if sample.awg3_dropped:
        if not dropped:
            moment = sample.at.isoformat()
            report_trouble(Event.AWG3_DROPPED, {**data, "since": moment}, key=prefix + moment)
        return
    has_awg2 = any(c["name"] == AWG2.name for c in sample.containers)
    for key in dropped:
        if not has_awg2:
            forget_trouble(key)
            continue
        moment = key.removeprefix(prefix)
        report_recovery(
            key,
            Event.AWG3_RESTORED,
            {**data, "since": moment},
            key=f"{Event.AWG3_RESTORED}:{server.pk}:{moment}",
        )


def _last_seen(server: Server) -> str:
    return server.last_seen_at.isoformat() if server.last_seen_at else ""


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
        containers=[_container(c, metrics) for c in metrics.containers],
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


def _container(container: ContainerState, metrics: HostMetrics) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": container.name, "state": container.state}
    if metrics.awg3_dropped and container.name == AWG2.name:
        entry["awg3_dropped"] = True
    return entry
