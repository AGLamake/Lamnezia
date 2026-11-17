from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _media(settings: Any, tmp_path: Path) -> None:
    """Screenshots of the tests land in a temporary folder."""
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture(autouse=True)
def _cache() -> Iterator[None]:
    """Tokens of sent messages (SendOnce) do not leak from one test to another."""
    cache.clear()
    yield
    cache.clear()
