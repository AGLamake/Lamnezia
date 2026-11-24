"""Dump of the panel database for the panel archive and its load back: `pg_dump`, `pg_restore`."""

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from django.conf import settings

from apps.backups.errors import PanelArchiveError, PanelArchiveErrorCode

_POSTGRES_ENGINE = "django.db.backends.postgresql"
# Last lines of a client's complaint kept for the logs; it never prints the password.
_STDERR_TAIL = 2000
# The tables of the panel go first: the database must hold the archive and nothing else, or
# migrations of a newer panel would meet tables they are about to create. The notice that lists
# every dropped table stays quiet: psql's stderr is read only once the dump is sent.
_EMPTY_SCHEMA = (
    b"SET client_min_messages = warning;\nDROP SCHEMA public CASCADE;\nCREATE SCHEMA public;\n"
)


class DatabaseDump(Protocol):
    def write(self, path: Path) -> None:
        """Save the whole database into `path`."""


class DatabaseLoad(Protocol):
    def load(self, path: Path) -> None:
        """Replace the whole database with the dump in `path`, or leave it as it was."""


class _PostgresClient:
    """Connection of the PostgreSQL command line tools to the database Django uses."""

    def __init__(self, database: Mapping[str, Any]) -> None:
        if database["ENGINE"] != _POSTGRES_ENGINE:
            raise PanelArchiveError(PanelArchiveErrorCode.NOT_POSTGRES, database["ENGINE"])
        self._database = database

    def _connection(self) -> list[str]:
        db = self._database
        return [
            f"--host={db['HOST']}",
            f"--port={db['PORT'] or 5432}",
            f"--username={db['USER']}",
            f"--dbname={db['NAME']}",
        ]

    def _environment(self) -> dict[str, str]:
        return {**os.environ, "PGPASSWORD": str(self._database["PASSWORD"])}


class PostgresDump(_PostgresClient):
    """`pg_dump --format=custom` of the database Django uses; `pg_restore` puts it back.

    Owners and grants are left out: the restored database belongs to whoever restores it.
    """

    @classmethod
    def from_settings(cls) -> "PostgresDump":
        return cls(settings.DATABASES["default"])

    def write(self, path: Path) -> None:
        command = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges", f"--file={path}"]
        try:
            done = subprocess.run(
                command + self._connection(),
                env=self._environment(),
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise PanelArchiveError(PanelArchiveErrorCode.DUMP_FAILED, "no pg_dump") from exc
        if done.returncode != 0:
            detail = done.stderr[-_STDERR_TAIL:]
            raise PanelArchiveError(PanelArchiveErrorCode.DUMP_FAILED, detail)


class PostgresLoad(_PostgresClient):
    """Loads a `pg_dump --format=custom` file into the database Django uses, in one transaction.

    `pg_restore` turns the dump into SQL and `psql --single-transaction` runs it after emptying
    the schema: any error rolls everything back, and other connections see either the old
    database or the new one.
    """

    @classmethod
    def from_settings(cls) -> "PostgresLoad":
        return cls(settings.DATABASES["default"])

    def load(self, path: Path) -> None:
        restore_command = ["pg_restore", "--no-owner", "--no-privileges", "--file=-", str(path)]
        psql_command = ["psql", "--quiet", "--no-psqlrc", "--single-transaction"]
        psql_command += ["--set=ON_ERROR_STOP=1", *self._connection()]
        try:
            with (
                subprocess.Popen(
                    psql_command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    env=self._environment(),
                ) as psql,
                subprocess.Popen(
                    restore_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                ) as restore,
            ):
                errors = self._feed(psql, restore)
        except FileNotFoundError as exc:
            raise PanelArchiveError(PanelArchiveErrorCode.LOAD_FAILED, str(exc)) from exc
        if restore.returncode != 0 or psql.returncode != 0:
            raise PanelArchiveError(PanelArchiveErrorCode.LOAD_FAILED, errors[-_STDERR_TAIL:])

    @staticmethod
    def _feed(psql: subprocess.Popen[bytes], restore: subprocess.Popen[bytes]) -> str:
        """Send the emptying SQL and the dump into psql; return what both said on stderr."""
        assert psql.stdin is not None and restore.stdout is not None and restore.stderr is not None
        try:
            psql.stdin.write(_EMPTY_SCHEMA)
            shutil.copyfileobj(restore.stdout, psql.stdin)
        except BrokenPipeError:
            restore.kill()  # psql stopped at an error: the rest of the dump is not needed
        finally:
            psql.stdin.close()
        restore_errors = restore.stderr.read()
        _, psql_errors = psql.communicate()
        restore.wait()
        return (restore_errors + psql_errors).decode(errors="replace")
