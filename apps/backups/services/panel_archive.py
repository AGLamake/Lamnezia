"""Contents of the panel archive: the database, .env, uploaded files, written and checked.

Inside the encryption (`archive_cipher`) the archive is a tar.gz that ``deploy/archive/unpack.sh``
unpacks on a new machine:

- ``manifest.json`` — format, panel version and time of the archive;
- ``env`` — the .env of the panel, rebuilt from its ``LAMNEZIA_*`` and ``COMPOSE_*`` variables
  but the password of scheduled archives;
- ``migrations.txt`` — the applied migration files, so an older panel refuses a newer archive;
- ``database.dump`` — ``pg_dump --format=custom``;
- ``media/`` — the uploaded files;
- ``SHA256SUMS`` — checksums of the files above but the media, in ``sha256sum`` format.
"""

import hashlib
import io
import json
import re
import tarfile
import tempfile
import tomllib
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from apps.backups.errors import PanelArchiveError, PanelArchiveErrorCode
from apps.backups.services.archive_cipher import (
    ArchiveCipher,
    ArchiveDecryptionError,
    DamagedArchiveError,
    DecryptingReader,
    WrongPasswordError,
)
from apps.backups.services.database_dump import DatabaseDump

FORMAT = 1
MANIFEST, ENV, MIGRATIONS, DATABASE, MEDIA, SUMS = (
    "manifest.json",
    "env",
    "migrations.txt",
    "database.dump",
    "media",
    "SHA256SUMS",
)
_ENV_PREFIXES = ("LAMNEZIA_", "COMPOSE_")
ARCHIVE_PASSWORD_VARIABLE = "LAMNEZIA_PANEL_ARCHIVE_PASSWORD"
# compose.yaml derives the URLs and hosts from other variables, and the database URL carries its
# password; the password of scheduled archives would open every archive made with it.
_NOT_ARCHIVED = frozenset(
    {
        "LAMNEZIA_ALLOWED_HOSTS",
        "LAMNEZIA_DATABASE_URL",
        "LAMNEZIA_REDIS_URL",
        ARCHIVE_PASSWORD_VARIABLE,
    }
)
_PLAIN_VALUE = re.compile(r"[\w@%+=:,./-]*")
DUMP_DIR_PREFIX = ".dump-"


def archived_variable(name: str) -> bool:
    """Whether a variable of the environment goes into the archived .env."""
    return name.startswith(_ENV_PREFIXES) and name not in _NOT_ARCHIVED


def env_file(environ: Mapping[str, str]) -> str:
    """The .env of the panel from its environment: docker compose passed the file in whole.

    The password of scheduled archives stays out: an archive must not hold its own key.
    """
    lines = [
        f"{name}={_env_value(value)}"
        for name, value in sorted(environ.items())
        if archived_variable(name)
    ]
    return "\n".join(lines) + "\n"


def _env_value(value: str) -> str:
    """The value quoted so that docker compose reads it back unchanged, `$` included."""
    if _PLAIN_VALUE.fullmatch(value):
        return value
    if "'" not in value and "\n" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return '"' + escaped.replace("$", "$$") + '"'


def read_env(text: str) -> dict[str, str]:
    """Variables of an archived .env, read back the way `env_file` quoted them."""
    variables = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if not sep or not name or name.startswith("#"):
            continue
        variables[name] = _unquoted(value)
    return variables


def _unquoted(value: str) -> str:
    quote = value[:1]
    if quote not in ("'", '"') or len(value) < len("''") or value[-1] != quote:
        return value
    if quote == "'":
        return value[1:-1]
    return re.sub(r"\\(.)", _escaped, value[1:-1]).replace("$$", "$")


def _escaped(match: re.Match[str]) -> str:
    char = match.group(1)
    return "\n" if char == "n" else char


def migration_files() -> list[str]:
    """Applied migrations of the panel's own apps, as file paths from the code root."""
    paths = []
    for label, name in sorted(MigrationRecorder(connection).applied_migrations()):
        try:
            app_path = Path(apps.get_app_config(label).path)
        except LookupError:
            continue
        file = app_path / "migrations" / f"{name}.py"
        if "site-packages" not in file.parts and file.is_relative_to(settings.BASE_DIR):
            paths.append(file.relative_to(settings.BASE_DIR).as_posix())
    return paths


def newer_migrations(listed: str) -> list[str]:
    """Migration files of an archive's list that this code lacks: the archive is newer."""
    return [
        path
        for path in listed.splitlines()
        if path.strip() and not (settings.BASE_DIR / path.strip()).is_file()
    ]


def panel_version() -> str:
    with (settings.BASE_DIR / "pyproject.toml").open("rb") as file:
        return str(tomllib.load(file)["project"]["version"])


@dataclass(frozen=True)
class Manifest:
    """What an archive says about itself."""

    created_at: datetime
    panel_version: str
    format: int = FORMAT

    def to_json(self) -> bytes:
        data = {
            "format": self.format,
            "panel_version": self.panel_version,
            "created_at": self.created_at.isoformat(),
        }
        return json.dumps(data, indent=2).encode() + b"\n"

    @classmethod
    def from_json(cls, raw: bytes) -> "Manifest":
        data = json.loads(raw)
        created_at = datetime.fromisoformat(data["created_at"])
        return cls(created_at, data["panel_version"], int(data["format"]))


class PanelArchiveWriter:
    """Writes one archive of the panel as it is now."""

    def __init__(
        self, password: str, dump: DatabaseDump, environ: Mapping[str, str], media_root: Path
    ) -> None:
        self._cipher = ArchiveCipher(password)
        self._dump = dump
        self._environ = environ
        self._media_root = media_root

    def write(self, target: Path) -> None:
        """Write the archive into the new file `target`; the dump waits beside it meanwhile."""
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=DUMP_DIR_PREFIX) as work:
            dump = Path(work) / DATABASE
            self._dump.write(dump)
            with target.open("xb") as raw:
                stream = self._cipher.writer(raw)
                with tarfile.open(fileobj=stream, mode="w|gz") as tar:  # type: ignore[call-overload]
                    self._fill(tar, dump)
                stream.close()

    def _fill(self, tar: tarfile.TarFile, dump: Path) -> None:
        sums = []
        manifest = Manifest(timezone.now(), panel_version())
        for name, data in (
            (MANIFEST, manifest.to_json()),
            (ENV, env_file(self._environ).encode()),
            (MIGRATIONS, "".join(f"{path}\n" for path in migration_files()).encode()),
        ):
            _add_bytes(tar, name, data)
            sums.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        tar.add(dump, arcname=DATABASE)
        sums.append(f"{_file_digest(dump)}  {DATABASE}\n")
        if self._media_root.is_dir():
            tar.add(self._media_root, arcname=MEDIA)
        _add_bytes(tar, SUMS, "".join(sums).encode())


class PanelArchiveReader:
    """Reads an archive through to its end and checks it: the password and the checksums.

    A format 2 archive tells a wrong password from a damaged file; format 1 fails as unreadable.
    """

    _UNREADABLE = (
        ArchiveDecryptionError,
        tarfile.TarError,
        zlib.error,
        EOFError,
        OSError,
        KeyError,
        ValueError,
    )

    def __init__(self, password: str) -> None:
        self._cipher = ArchiveCipher(password)

    def check(self, path: Path) -> Manifest:
        failure = PanelArchiveErrorCode.UNREADABLE
        try:
            with path.open("rb") as raw:
                stream = self._cipher.reader(raw)
                if stream.authenticated:
                    failure = PanelArchiveErrorCode.DAMAGED
                digests, kept = self._digests(stream)
            sums = kept[SUMS].decode()
            manifest = Manifest.from_json(kept[MANIFEST])
        except self._UNREADABLE as exc:
            raise PanelArchiveError(self._code(exc, failure), repr(exc)) from exc
        expected = dict(line.split("  ", 1)[::-1] for line in sums.splitlines())
        if any(digests.get(name) != digest for name, digest in expected.items()):
            raise PanelArchiveError(failure, "checksum mismatch")
        return manifest

    def unpack(self, path: Path, target: Path) -> Manifest:
        """Check the archive, then unpack it into the empty folder `target`.

        The unpacked files are checked against the sums once more: the archive could change
        between the two readings.
        """
        manifest = self.check(path)
        try:
            with path.open("rb") as raw:
                stream = self._cipher.reader(raw)
                with tarfile.open(fileobj=stream, mode="r|gz") as tar:  # type: ignore[call-overload]
                    tar.extractall(target, filter="data")
            sums = (target / SUMS).read_text()
        except self._UNREADABLE as exc:
            raise PanelArchiveError(PanelArchiveErrorCode.DAMAGED, repr(exc)) from exc
        for line in sums.splitlines():
            digest, name = line.split("  ", 1)
            file = target / name
            if not file.is_file() or _file_digest(file) != digest:
                raise PanelArchiveError(PanelArchiveErrorCode.DAMAGED, f"{name} differs")
        if manifest.format != FORMAT:
            raise PanelArchiveError(PanelArchiveErrorCode.UNKNOWN_FORMAT, str(manifest.format))
        return manifest

    @staticmethod
    def _code(exc: Exception, failure: PanelArchiveErrorCode) -> PanelArchiveErrorCode:
        if isinstance(exc, WrongPasswordError):
            return PanelArchiveErrorCode.WRONG_PASSWORD
        if isinstance(exc, DamagedArchiveError):
            return PanelArchiveErrorCode.DAMAGED
        return failure

    @staticmethod
    def _digests(stream: DecryptingReader) -> tuple[dict[str, str], dict[str, bytes]]:
        """SHA-256 of every file but the media, and the manifest and the sums themselves."""
        digests: dict[str, str] = {}
        kept: dict[str, bytes] = {}
        with tarfile.open(fileobj=stream, mode="r|gz") as tar:  # type: ignore[call-overload]
            for member in tar:
                content = tar.extractfile(member)
                if content is None or member.name.startswith(f"{MEDIA}/"):
                    continue
                if member.name in (SUMS, MANIFEST):
                    kept[member.name] = data = content.read()
                    digests[member.name] = hashlib.sha256(data).hexdigest()
                else:
                    digests[member.name] = hashlib.file_digest(content, "sha256").hexdigest()
        return digests, kept


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(timezone.now().timestamp())
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))


def _file_digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()
