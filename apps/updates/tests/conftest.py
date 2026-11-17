from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.core.cache import cache

from apps.updates.services import updates
from apps.updates.services.versions import Version

RUNNING = Version(1, 0, 0)


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    """The saved release and the start lock live in the cache, which outlives a test."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def running(monkeypatch: pytest.MonkeyPatch) -> Version:
    """The panel runs 1.0.0, whatever pyproject.toml says now."""
    monkeypatch.setattr(updates, "running_version", lambda: RUNNING)
    return RUNNING


@pytest.fixture
def update_dir(settings: Any, tmp_path: Path) -> Path:
    """The folders install.sh makes, with the host unit installed (the agent file)."""
    root = tmp_path / "update"
    (root / "request").mkdir(parents=True)
    (root / "status").mkdir()
    (root / "status" / "agent").write_text("releases\n")
    settings.UPDATE_DIR = root
    return root
