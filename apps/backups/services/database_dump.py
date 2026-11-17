"""Dump of the panel database for the panel archive: `pg_dump` in its custom format."""

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from django.conf import settings

from apps.backups.errors import PanelArchiveError, PanelArchiveErrorCode

_POSTGRES_ENGINE = "django.db.backends.postgresql"
# Last lines of pg_dump's complaint kept for the logs; it never prints the password.
_STDERR_TAIL = 2000


class DatabaseDump(Protocol):
    def write(self, path: Path) -> None:
        """Save the whole database into `path`."""


class PostgresDump:
    """`pg_dump --format=custom` of the database Django uses; `pg_restore` puts it back.

    Owners and grants are left out: the restored database belongs to whoever restores it.
    """

    def __init__(self, database: Mapping[str, Any]) -> None:
        if database["ENGINE"] != _POSTGRES_ENGINE:
            raise PanelArchiveError(PanelArchiveErrorCode.NOT_POSTGRES, database["ENGINE"])
        self._database = database

    @classmethod
    def from_settings(cls) -> "PostgresDump":
        return cls(settings.DATABASES["default"])

    def write(self, path: Path) -> None:
        db = self._database
        command = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges", f"--file={path}"]
        command += [f"--host={db['HOST']}", f"--port={db['PORT'] or 5432}"]
        command += [f"--username={db['USER']}", f"--dbname={db['NAME']}"]
        environment = {**os.environ, "PGPASSWORD": str(db["PASSWORD"])}
        try:
            done = subprocess.run(
                command, env=environment, capture_output=True, text=True, check=False
            )
        except FileNotFoundError as exc:
            raise PanelArchiveError(PanelArchiveErrorCode.DUMP_FAILED, "no pg_dump") from exc
        if done.returncode != 0:
            detail = done.stderr[-_STDERR_TAIL:]
            raise PanelArchiveError(PanelArchiveErrorCode.DUMP_FAILED, detail)
