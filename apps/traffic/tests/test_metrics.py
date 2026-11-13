from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.test import Client

from apps.traffic.models import TrafficCounter, TrafficHourly
from apps.traffic.services.metrics import Metric, TrafficMetrics, render
from apps.traffic.services.online import OnlineRule

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
ONLINE = OnlineRule(timedelta(minutes=3), timedelta(minutes=8))
TOKEN = "scrape-token"


def counter(source: str, key: str, handshake: datetime | None, **fields: Any) -> None:
    TrafficCounter.objects.create(
        source=source, public_key=key, latest_handshake=handshake, collected_at=NOW, **fields
    )


def hourly(source: str, key: str, hour: int, rx: int, tx: int) -> None:
    TrafficHourly.objects.create(
        source=source, public_key=key, hour=NOW.replace(hour=hour), rx_bytes=rx, tx_bytes=tx
    )


def collected() -> dict[str, dict[str, int | float]]:
    return {metric.name: metric.values for metric in TrafficMetrics(NOW, ONLINE).collect()}


def test_traffic_is_summed_per_source_over_keys_and_hours() -> None:
    hourly("awg:1", "alice", 10, 100, 1000)
    hourly("awg:1", "alice", 11, 5_000_000_000, 7)
    hourly("awg:1", "bob", 11, 1, 2)
    hourly("awg:2", "carol", 11, 30, 40)

    metrics = collected()

    assert metrics["lamnezia_traffic_received_bytes_total"] == {
        "awg:1": 5_000_000_101,
        "awg:2": 30,
    }
    assert metrics["lamnezia_traffic_sent_bytes_total"] == {"awg:1": 1009, "awg:2": 40}


def test_online_counts_only_recent_handshakes() -> None:
    counter("awg:1", "alice", NOW - timedelta(minutes=1))
    counter("awg:1", "bob", NOW - timedelta(minutes=10))
    counter("awg:1", "carol", None)

    metrics = collected()

    assert metrics["lamnezia_peers"] == {"awg:1": 3}
    assert metrics["lamnezia_peers_online"] == {"awg:1": 1}
    assert metrics["lamnezia_traffic_collected_timestamp_seconds"] == {"awg:1": NOW.timestamp()}


def test_peers_count_only_present_ones_and_xray_is_online_by_traffic() -> None:
    counter("xray:1", "alice", None, active_at=NOW - timedelta(minutes=5))
    counter("xray:1", "bob", None, active_at=NOW - timedelta(minutes=20))
    counter("xray:1", "carol", None, active_at=NOW, present=False)

    metrics = collected()

    assert metrics["lamnezia_peers"] == {"xray:1": 2}
    assert metrics["lamnezia_peers_online"] == {"xray:1": 1}


def test_render_writes_the_prometheus_text_format() -> None:
    metric = Metric("lamnezia_peers", "gauge", "Peers.", {'b"\\': 2, "a": 1})

    assert render([metric]) == (
        "# HELP lamnezia_peers Peers.\n"
        "# TYPE lamnezia_peers gauge\n"
        'lamnezia_peers{source="a"} 1\n'
        'lamnezia_peers{source="b\\"\\\\"} 2\n'
    )


def test_endpoint_is_off_without_a_configured_token(client: Client, settings: Any) -> None:
    settings.METRICS_TOKEN = ""

    assert client.get("/metrics", headers={"Authorization": "Bearer "}).status_code == 404


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", TOKEN, f"Basic {TOKEN}"])
def test_endpoint_refuses_a_missing_or_wrong_token(
    client: Client, settings: Any, authorization: str | None
) -> None:
    settings.METRICS_TOKEN = TOKEN
    headers = {"Authorization": authorization} if authorization else {}

    assert client.get("/metrics", headers=headers).status_code == 403


def test_endpoint_serves_metrics_with_the_token(client: Client, settings: Any) -> None:
    settings.METRICS_TOKEN = TOKEN
    hourly("awg:1", "alice", 10, 100, 200)

    response = client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain; version=0.0.4")
    assert 'lamnezia_traffic_sent_bytes_total{source="awg:1"} 200' in response.content.decode()
