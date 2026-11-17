"""Test helpers of the panel archive: a stand-in for pg_dump and the folder under test."""

from pathlib import Path

from django.conf import settings

from apps.backups.services.panel_archives import PanelArchiveFolder

DUMP = b"PGDMP fake custom dump"
PASSWORD = "archive password 1"


class FakeDump:
    def __init__(self, content: bytes = DUMP) -> None:
        self.content = content

    def write(self, path: Path) -> None:
        path.write_bytes(self.content)


def folder(keep: int = 3) -> PanelArchiveFolder:
    return PanelArchiveFolder(settings.PANEL_ARCHIVE_DIR, keep)
