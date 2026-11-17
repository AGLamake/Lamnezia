from pathlib import Path
from typing import Any

import pytest

from apps.backups.services.database_dump import PostgresDump
from apps.backups.tests.panel_archive_helpers import FakeDump


@pytest.fixture
def archive_settings(settings: Any, tmp_path: Path) -> Path:
    """Panel archives and uploaded files in a temp directory."""
    settings.PANEL_ARCHIVE_DIR = tmp_path / "archives"
    settings.MEDIA_ROOT = tmp_path / "media"
    (tmp_path / "media" / "logos").mkdir(parents=True)
    (tmp_path / "media" / "logos" / "logo.png").write_bytes(b"png")
    return tmp_path


@pytest.fixture
def fake_dump(monkeypatch: pytest.MonkeyPatch) -> FakeDump:
    """The database dump of the panel without PostgreSQL."""
    dump = FakeDump()
    monkeypatch.setattr(PostgresDump, "from_settings", staticmethod(lambda: dump))
    return dump
