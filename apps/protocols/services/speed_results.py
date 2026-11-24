"""The saved outcomes of browser speed tests: recording a port, the summary by port for the server
card and removing the old ones.

Only the server, the port and the speeds are kept: not who measured, nor their address or network.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import fmean

from apps.protocols.models import SpeedTestResult
from apps.protocols.services.speed_test import RunningTest
from apps.servers.models import Server

# How long the outcomes are kept and summed up on the card.
KEEP = timedelta(days=90)
# A speed above this in Mbit/s is not a measurement: the page would never send it.
MAX_MBITS = 100_000.0


@dataclass(frozen=True)
class MeasuredPort:
    """What the page measured on one port: no answer, or the speeds in Mbit/s it got to."""

    port: int
    reachable: bool
    download: float | None = None
    upload: float | None = None


def record(server: Server, running: RunningTest, measured: MeasuredPort) -> SpeedTestResult:
    """Save the outcome of a port of the running test; measuring it again replaces the row."""
    if running.operation_id is None:
        raise ValueError("the test has no start operation")
    result, _created = SpeedTestResult.objects.update_or_create(
        test=running.operation_id,
        port=measured.port,
        defaults={
            "server": server,
            "mode": running.plan.mode,
            "transport": ",".join(running.plan.server.transports),
            "reachable": measured.reachable,
            "download": measured.download if measured.reachable else None,
            "upload": measured.upload if measured.reachable else None,
        },
    )
    return result


@dataclass(frozen=True)
class PortSummary:
    """The tests of one port within KEEP: how many, how many got no answer, average speeds."""

    port: int
    tests: int
    unreachable: int
    download: float | None
    upload: float | None
    last: datetime


def summary(server: Server, now: datetime) -> list[PortSummary]:
    """One row per port the server was measured on, by port."""
    by_port: dict[int, list[SpeedTestResult]] = defaultdict(list)
    for result in SpeedTestResult.objects.filter(server=server, measured_at__gte=now - KEEP):
        by_port[result.port].append(result)
    return [_summed(port, results) for port, results in sorted(by_port.items())]


def _summed(port: int, results: list[SpeedTestResult]) -> PortSummary:
    downloads = [result.download for result in results if result.download is not None]
    uploads = [result.upload for result in results if result.upload is not None]
    return PortSummary(
        port=port,
        tests=len(results),
        unreachable=sum(not result.reachable for result in results),
        download=fmean(downloads) if downloads else None,
        upload=fmean(uploads) if uploads else None,
        last=max(result.measured_at for result in results),
    )


def prune(before: datetime) -> int:
    """Delete the outcomes measured before `before`; return how many were deleted."""
    deleted, _by_model = SpeedTestResult.objects.filter(measured_at__lt=before).delete()
    return deleted
