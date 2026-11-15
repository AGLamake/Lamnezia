import pytest

from apps.traffic.models import TrafficCounter, TrafficHourly
from apps.traffic.services.moving import move_key_traffic

pytestmark = pytest.mark.django_db

HOUR = "2026-11-15T10:00Z"
NEXT_HOUR = "2026-11-15T11:00Z"


def test_hours_move_and_an_hour_both_sources_have_is_added_up() -> None:
    TrafficHourly.objects.create(source="awg:1", public_key="k", hour=HOUR, rx_bytes=5, tx_bytes=1)
    TrafficHourly.objects.create(source="awg:1", public_key="k", hour=NEXT_HOUR, rx_bytes=7)
    TrafficHourly.objects.create(source="awg:2", public_key="k", hour=HOUR, rx_bytes=3, tx_bytes=2)

    move_key_traffic("awg:1", "awg:2")

    rows = TrafficHourly.objects.order_by("hour").values_list("source", "rx_bytes", "tx_bytes")
    assert list(rows) == [("awg:2", 8, 3), ("awg:2", 7, 0)]


def test_counters_keep_their_server_and_count_for_the_new_source() -> None:
    TrafficCounter.objects.create(source="awg:1", public_key="k", collected_at=HOUR)
    TrafficCounter.objects.create(
        source="awg:3", key_source="awg:1", public_key="k", collected_at=HOUR
    )

    move_key_traffic("awg:1", "awg:2")

    counters = TrafficCounter.objects.order_by("source").values_list("source", "key_source")
    assert list(counters) == [("awg:1", "awg:2"), ("awg:3", "awg:2")]
