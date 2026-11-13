"""Traffic and online peers in the Prometheus text format, one series per traffic source."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Count, Max, Q, Sum

from apps.traffic.models import TrafficCounter, TrafficHourly
from apps.traffic.services.online import OnlineRule


@dataclass(frozen=True)
class Metric:
    """One metric family; `values` maps a traffic source to its value."""

    name: str
    kind: str
    help: str
    values: dict[str, int | float]

    def render(self) -> Iterator[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} {self.kind}"
        for source, value in sorted(self.values.items()):
            yield f'{self.name}{{source="{_escape(source)}"}} {value}'


class TrafficMetrics:
    """Totals for the whole history, not per key: the scraper does not need to see key ids.

    `lamnezia_traffic_collected_timestamp_seconds` falling behind shows a server the panel can
    no longer read.
    """

    def __init__(self, now: datetime, online: OnlineRule) -> None:
        self._now = now
        self._online = online

    def collect(self) -> list[Metric]:
        return [*self._traffic(), *self._peers()]

    def _traffic(self) -> list[Metric]:
        totals = TrafficHourly.objects.values("source").annotate(
            rx=Sum("rx_bytes"), tx=Sum("tx_bytes")
        )
        return [
            Metric(
                "lamnezia_traffic_received_bytes_total",
                "counter",
                "Bytes the server received from clients.",
                {row["source"]: row["rx"] for row in totals},
            ),
            Metric(
                "lamnezia_traffic_sent_bytes_total",
                "counter",
                "Bytes the server sent to clients.",
                {row["source"]: row["tx"] for row in totals},
            ),
        ]

    def _peers(self) -> list[Metric]:
        peers = TrafficCounter.objects.values("source").annotate(
            total=Count("id", filter=Q(present=True)),
            online=Count("id", filter=self._online.filter(self._now)),
            collected=Max("collected_at"),
        )
        return [
            Metric(
                "lamnezia_peers",
                "gauge",
                "Peers seen in the latest reading.",
                {row["source"]: row["total"] for row in peers},
            ),
            Metric(
                "lamnezia_peers_online",
                "gauge",
                "Peers with a recent handshake or, without one (XRay), recent traffic.",
                {row["source"]: row["online"] for row in peers},
            ),
            Metric(
                "lamnezia_traffic_collected_timestamp_seconds",
                "gauge",
                "Unix time of the latest successful reading.",
                {row["source"]: row["collected"].timestamp() for row in peers},
            ),
        ]


def render(metrics: Iterable[Metric]) -> str:
    return "".join(f"{line}\n" for metric in metrics for line in metric.render())


def _escape(label: str) -> str:
    return label.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")
