"""Template filters of the server pages."""

import pytest

from apps.servers.templatetags.servers import load_tone


@pytest.mark.parametrize(
    ("percent", "tone"),
    [
        (None, ""),
        (10.0, ""),
        (74.9, ""),
        (75.0, "progress-bar-warning"),
        (89.9, "progress-bar-warning"),
        (90.0, "progress-bar-danger"),
        (100.0, "progress-bar-danger"),
    ],
)
def test_load_turns_amber_then_red(percent: float | None, tone: str) -> None:
    assert load_tone(percent) == tone
