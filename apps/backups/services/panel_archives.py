"""Panel archives kept on the panel: making one, the list, download and removal, the operation.

The folder is the `archives` volume shared by the web and worker containers. An archive gets its
name only when it is complete, so the list never shows a file cut short.
"""

import os
import re
import secrets
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.backups.services.database_dump import DatabaseDump, PostgresDump
from apps.backups.services.panel_archive import DUMP_DIR_PREFIX, PanelArchiveWriter
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch
from apps.ssh.encryption import SecretCipher

PANEL_ARCHIVE_KIND = "backups.panel_archive"
PANEL_ARCHIVE_DOWNLOAD = "backups.download_panel_archive"
PANEL_ARCHIVE_DELETE = "backups.delete_panel_archive"
PASSWORD_PARAM = "password_token"
# A scheduled archive takes the password from the settings in the worker, never in its params.
SCHEDULED_PARAM = "scheduled"
_NAME = re.compile(r"lamnezia-\d{8}-\d{6}\.lamnezia")
_STAGING_PREFIX = ".staging-"
# An archive uploaded to restore waits under such a name until its operation takes it.
UPLOAD_PREFIX = ".upload-"
_UPLOAD = re.compile(rf"{re.escape(UPLOAD_PREFIX)}[0-9a-f]{{32}}")
_LEFTOVER_PREFIXES = (_STAGING_PREFIX, DUMP_DIR_PREFIX, UPLOAD_PREFIX)


@dataclass(frozen=True)
class ArchiveFile:
    """A complete archive in the folder."""

    path: Path
    size: int
    created_at: datetime

    @property
    def name(self) -> str:
        return self.path.name


class ArchiveNotFoundError(LookupError):
    pass


class PanelArchiveFolder:
    """The folder of archives: the newest `keep` stay, older ones go when a new one is made."""

    def __init__(self, root: Path, keep: int) -> None:
        self._root = root
        self._keep = keep

    @classmethod
    def from_settings(cls) -> "PanelArchiveFolder":
        return cls(settings.PANEL_ARCHIVE_DIR, settings.PANEL_ARCHIVE_KEEP)

    def archives(self) -> list[ArchiveFile]:
        """Complete archives, the newest first."""
        if not self._root.is_dir():
            return []
        files = [path for path in self._root.iterdir() if _NAME.fullmatch(path.name)]
        return sorted((_archive_file(path) for path in files), key=lambda a: a.name, reverse=True)

    def get(self, name: str) -> ArchiveFile:
        path = self._root / name
        if not _NAME.fullmatch(name) or not path.is_file():
            raise ArchiveNotFoundError(name)
        return _archive_file(path)

    def delete(self, name: str) -> None:
        self.get(name).path.unlink(missing_ok=True)

    def upload(self, chunks: Iterable[bytes]) -> str:
        """Keep an archive uploaded to restore it; returns the name its operation takes."""
        self._root.mkdir(parents=True, exist_ok=True)
        self._remove_leftovers()
        name = f"{UPLOAD_PREFIX}{secrets.token_hex(16)}"
        path = self._root / name
        with path.open("xb") as file:
            path.chmod(0o600)
            for chunk in chunks:
                file.write(chunk)
        return name

    def restorable(self, name: str) -> Path:
        """The file of a complete archive or of an uploaded one, to restore it."""
        if _UPLOAD.fullmatch(name) and (self._root / name).is_file():
            return self._root / name
        return self.get(name).path

    def work_dir(self) -> Path:
        """A new folder for an unpacked archive, removed as a leftover if the worker dies."""
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / f"{DUMP_DIR_PREFIX}{secrets.token_hex(8)}"
        path.mkdir(mode=0o700)
        return path

    def create(
        self, password: str, dump: DatabaseDump | None = None, *, prune: bool = True
    ) -> tuple[ArchiveFile, int]:
        """Make an archive of the panel now; returns it and how many old ones were removed.

        Without `prune` the old archives stay, e.g. the one a restore is bringing in.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        self._remove_leftovers()
        name = timezone.now().astimezone(UTC).strftime("lamnezia-%Y%m%d-%H%M%S.lamnezia")
        staging = self._root / f"{_STAGING_PREFIX}{name}"
        writer = PanelArchiveWriter(
            password, dump or PostgresDump.from_settings(), os.environ, settings.MEDIA_ROOT
        )
        try:
            writer.write(staging)
            staging.chmod(0o600)
            staging.replace(self._root / name)
        finally:
            staging.unlink(missing_ok=True)
        return self.get(name), self._prune() if prune else 0

    def _remove_leftovers(self) -> None:
        """Files of an archive whose worker died; the unencrypted dump must not stay on disk."""
        deadline = timezone.now().timestamp() - settings.OPERATION_TIMEOUT
        for path in self._root.iterdir():
            if path.name.startswith(_LEFTOVER_PREFIXES) and path.stat().st_mtime < deadline:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)

    def _prune(self) -> int:
        old = self.archives()[self._keep :]
        for archive in old:
            archive.path.unlink(missing_ok=True)
        return len(old)


def _archive_file(path: Path) -> ArchiveFile:
    stat = path.stat()
    return ArchiveFile(path, stat.st_size, datetime.fromtimestamp(stat.st_mtime, tz=UTC))


def failed_archive_operation() -> Operation | None:
    """The latest finished archive operation when it failed; shown until one succeeds."""
    last = (
        Operation.objects.filter(
            kind=PANEL_ARCHIVE_KIND,
            status__in=(OperationStatus.SUCCEEDED, OperationStatus.FAILED),
        )
        .order_by("-created_at")
        .first()
    )
    return last if last is not None and last.status == OperationStatus.FAILED else None


def active_archive_operation() -> Operation | None:
    """The archive being made now, if any: one at a time is enough."""
    return (
        Operation.objects.filter(
            kind=PANEL_ARCHIVE_KIND,
            status__in=(OperationStatus.PENDING, OperationStatus.RUNNING),
        )
        .order_by("-created_at")
        .first()
    )


def start_panel_archive(password: str, user: User, ip: str | None) -> Operation:
    """Queue an archive; the password travels encrypted and the operation erases it on start.

    While another archive is being made, that one is returned instead of a second.
    """
    with transaction.atomic():
        active = active_archive_operation()
        if active is not None:
            return active
        token = SecretCipher.from_settings().encrypt(password)
        operation = launch(PANEL_ARCHIVE_KIND, {PASSWORD_PARAM: token}, user=user)
        audit.log(PANEL_ARCHIVE_KIND, actor=user, ip=ip)
    return operation


def start_scheduled_archive() -> Operation:
    """Queue an archive with the password of the settings, unless one is being made already."""
    with transaction.atomic():
        active = active_archive_operation()
        if active is not None:
            return active
        return launch(PANEL_ARCHIVE_KIND, {SCHEDULED_PARAM: True})


def log_download(archive: ArchiveFile, user: User, ip: str | None) -> None:
    audit.log(PANEL_ARCHIVE_DOWNLOAD, actor=user, data={"name": archive.name}, ip=ip)


def delete_archive(folder: PanelArchiveFolder, name: str, user: User, ip: str | None) -> None:
    folder.delete(name)
    audit.log(PANEL_ARCHIVE_DELETE, actor=user, data={"name": name}, ip=ip)
