from collections.abc import Iterator
from unittest.mock import patch

import pytest
from django.core.cache import cache

from apps.core.services.rate import RateWindow


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def test_hits_pass_up_to_the_limit() -> None:
    window = RateWindow("test:rate", 2, 60)

    assert [window.hit() for _ in range(3)] == [True, True, False]


def test_exhausted_tells_without_counting() -> None:
    window = RateWindow("test:rate", 2, 60)
    window.hit()

    assert not window.exhausted()
    assert not window.exhausted()
    window.hit()
    assert window.exhausted()


def test_keys_are_counted_apart() -> None:
    RateWindow("test:a", 1, 60).hit()

    assert RateWindow("test:b", 1, 60).hit()
    assert not RateWindow("test:a", 1, 60).hit()


def test_zero_means_no_limit_and_counts_nothing() -> None:
    window = RateWindow("test:rate", 0, 60)

    assert all(window.hit() for _ in range(5))
    assert not window.exhausted()
    assert cache.get("test:rate") is None


def test_the_first_hit_starts_a_window_of_its_seconds() -> None:
    RateWindow("test:rate", 5, 60).hit()

    with patch.object(cache, "add", wraps=cache.add) as add:
        RateWindow("test:rate", 5, 60).hit()

    add.assert_called_once_with("test:rate", 0, timeout=60)


def test_a_window_that_expired_before_the_count_starts_anew() -> None:
    window = RateWindow("test:rate", 1, 60)

    with patch.object(cache, "add"), patch.object(cache, "incr", side_effect=ValueError):
        assert window.hit()

    assert cache.get("test:rate") == 1
    assert not window.hit()
