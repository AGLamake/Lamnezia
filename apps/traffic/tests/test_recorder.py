import time
from datetime import UTC, datetime, timedelta

import pytest

from amnezia_core.awg_dump import PeerStats
from apps.traffic.models import TrafficCounter, TrafficHourly, TrafficSource
from apps.traffic.services.recorder import TrafficRecorder

pytestmark = pytest.mark.django_db

SOURCE = "awg:1"
ALICE = "alice-public-key"
START = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
STEP = timedelta(minutes=5)


def peer(rx: int, tx: int, key: str = ALICE, handshake: datetime | None = None) -> PeerStats:
    return PeerStats("awg0", key, None, (), handshake, rx, tx, None)


def readings(*values: tuple[int, int], key: str = ALICE) -> None:
    recorder = TrafficRecorder()
    for n, (rx, tx) in enumerate(values):
        recorder.record(SOURCE, [peer(rx, tx, key)], START + STEP * n)


def total(key: str = ALICE) -> tuple[int, int]:
    rows = TrafficHourly.objects.filter(source=SOURCE, public_key=key)
    return sum(r.rx_bytes for r in rows), sum(r.tx_bytes for r in rows)


def test_first_reading_is_only_a_baseline() -> None:
    readings((5000, 9000))

    assert total() == (0, 0)
    counter = TrafficCounter.objects.get()
    assert (counter.rx_bytes, counter.tx_bytes) == (5000, 9000)


def test_growth_is_added_as_delta() -> None:
    readings((1000, 2000), (1500, 2600), (1700, 3000))

    assert total() == (700, 1000)


def test_counter_reset_neither_loses_nor_doubles_traffic() -> None:
    # The container restarts between the 2nd and the 3rd reading: counters start from zero.
    readings((1000, 2000), (1200, 2500), (300, 400), (350, 700))

    assert total() == (200 + 300 + 50, 500 + 400 + 300)


def test_reset_of_one_direction_counts_as_reset() -> None:
    readings((1000, 2000), (100, 5000))

    assert total() == (100, 5000)


def test_missing_peer_counts_from_zero_when_it_returns() -> None:
    recorder = TrafficRecorder()
    recorder.record(SOURCE, [peer(1000, 1000)], START)
    recorder.record(SOURCE, [], START + STEP)  # disabled: removed from the server
    recorder.record(SOURCE, [peer(5000, 6000)], START + STEP * 2)

    assert total() == (5000, 6000)


def test_older_or_repeated_reading_is_ignored() -> None:
    recorder = TrafficRecorder()
    recorder.record(SOURCE, [peer(1000, 1000)], START)
    assert recorder.record(SOURCE, [peer(3000, 3000)], START + STEP)

    assert not recorder.record(SOURCE, [peer(2000, 2000)], START + STEP / 2)
    assert not recorder.record(SOURCE, [peer(3000, 3000)], START + STEP)
    assert total() == (2000, 2000)


def test_expected_peer_counts_from_the_first_byte() -> None:
    recorder = TrafficRecorder()
    recorder.expect_peer(SOURCE, ALICE, START)
    recorder.record(SOURCE, [peer(700, 900)], START + STEP)

    assert total() == (700, 900)


def test_traffic_goes_to_the_hour_of_the_reading() -> None:
    recorder = TrafficRecorder()
    recorder.record(SOURCE, [peer(0, 0)], START + timedelta(minutes=50))
    recorder.record(SOURCE, [peer(100, 100)], START + timedelta(minutes=55))
    recorder.record(SOURCE, [peer(300, 300)], START + timedelta(minutes=65))

    hours = dict(TrafficHourly.objects.values_list("hour", "rx_bytes"))
    assert hours == {START: 100, START + timedelta(hours=1): 200}


def test_sources_are_independent() -> None:
    recorder = TrafficRecorder()
    recorder.record(SOURCE, [peer(100, 100)], START)
    recorder.record("awg:2", [peer(100, 100)], START + STEP)
    recorder.record(SOURCE, [peer(200, 200)], START + STEP)

    assert total() == (100, 100)
    assert TrafficCounter.objects.get(source="awg:2").rx_bytes == 100


def test_growth_without_handshake_marks_activity() -> None:
    # XRay has no handshake: growth of the counters is the only sign of use.
    recorder = TrafficRecorder()
    recorder.record(SOURCE, [peer(100, 100)], START)
    recorder.record(SOURCE, [peer(200, 100)], START + STEP)
    recorder.record(SOURCE, [peer(200, 100)], START + STEP * 2)

    assert TrafficCounter.objects.get().active_at == START + STEP


def test_growth_with_a_handshake_leaves_activity_to_the_handshake() -> None:
    recorder = TrafficRecorder()
    recorder.record(SOURCE, [peer(100, 100, handshake=START)], START)
    recorder.record(SOURCE, [peer(200, 200, handshake=START + STEP)], START + STEP)

    assert TrafficCounter.objects.get().active_at is None


def test_missing_peer_is_not_present_until_it_returns() -> None:
    recorder = TrafficRecorder()
    recorder.record(SOURCE, [peer(100, 100)], START)
    recorder.record(SOURCE, [], START + STEP)
    assert not TrafficCounter.objects.get().present

    recorder.record(SOURCE, [peer(10, 10)], START + STEP * 2)
    assert TrafficCounter.objects.get().present


def test_restart_is_seen_by_the_start_time_when_counters_grew_past_the_old_ones() -> None:
    recorder = TrafficRecorder()
    recorder.record(SOURCE, [peer(100, 100)], START, "t1")
    recorder.record(SOURCE, [peer(150, 150)], START + STEP, "t1")
    # Restarted, then moved more than before the restart until the next reading.
    recorder.record(SOURCE, [peer(400, 500)], START + STEP * 2, "t2")

    assert total() == (50 + 400, 50 + 500)
    assert TrafficSource.objects.get().started == "t2"


def test_unknown_start_time_is_not_a_restart() -> None:
    recorder = TrafficRecorder()
    recorder.record(SOURCE, [peer(100, 100)], START, "t1")
    recorder.record(SOURCE, [peer(150, 150)], START + STEP)
    recorder.record(SOURCE, [peer(200, 200)], START + STEP * 2, "t1")

    assert total() == (100, 100)


def test_handshake_is_kept_when_the_reading_has_none() -> None:
    recorder = TrafficRecorder()
    recorder.record(SOURCE, [peer(0, 0, handshake=START)], START)
    recorder.record(SOURCE, [peer(0, 0)], START + STEP)

    assert TrafficCounter.objects.get().latest_handshake == START


def test_hundred_peers_take_less_than_five_seconds() -> None:
    recorder = TrafficRecorder()
    keys = [f"peer-{n}" for n in range(100)]

    started = time.monotonic()
    for n in range(3):
        peers = [peer(1000 * (n + 1), 2000 * (n + 1), key) for key in keys]
        recorder.record(SOURCE, peers, START + STEP * n)
    elapsed = time.monotonic() - started

    assert elapsed < 5
    assert TrafficHourly.objects.count() == 100
    assert total("peer-42") == (2000, 4000)
