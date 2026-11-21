import json
import os
import tarfile
from pathlib import Path

import pytest

from apps.backups.errors import PanelArchiveError, PanelArchiveErrorCode
from apps.backups.services.archive_cipher import ArchiveCipher
from apps.backups.services.database_dump import PostgresDump
from apps.backups.services.panel_archive import PanelArchiveReader, env_file
from apps.backups.services.panel_archives import ArchiveNotFoundError
from apps.backups.tests.panel_archive_helpers import DUMP, PASSWORD, FakeDump, downgrade, folder

pytestmark = pytest.mark.django_db


def members(path: Path, password: str = PASSWORD) -> dict[str, bytes]:
    with path.open("rb") as raw:
        stream = ArchiveCipher(password).reader(raw)
        with tarfile.open(fileobj=stream, mode="r|gz") as tar:  # type: ignore[call-overload]
            return {
                member.name: tar.extractfile(member).read() for member in tar if member.isfile()
            }


def test_the_archive_holds_the_dump_env_media_and_sums(
    archive_settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAMNEZIA_ENCRYPTION_KEY", "secret-key")
    archive, _ = folder().create(PASSWORD, FakeDump())

    content = members(archive.path)

    assert content["database.dump"] == DUMP
    assert content["media/logos/logo.png"] == b"png"
    assert "LAMNEZIA_ENCRYPTION_KEY=secret-key\n" in content["env"].decode()
    assert json.loads(content["manifest.json"])["format"] == 1
    assert "apps/backups/migrations/0001_initial.py" in content["migrations.txt"].decode()
    assert "  database.dump" in content["SHA256SUMS"].decode()
    if os.name != "nt":
        assert archive.path.stat().st_mode & 0o777 == 0o600


def test_the_reader_checks_password_and_sums(archive_settings: Path) -> None:
    archive, _ = folder().create(PASSWORD, FakeDump())

    assert PanelArchiveReader(PASSWORD).check(archive.path).panel_version

    with pytest.raises(PanelArchiveError) as error:
        PanelArchiveReader("another password").check(archive.path)
    assert error.value.code == PanelArchiveErrorCode.WRONG_PASSWORD


def test_a_file_cut_short_does_not_pass(archive_settings: Path) -> None:
    archive, _ = folder().create(PASSWORD, FakeDump(os.urandom(200_000)))
    archive.path.write_bytes(archive.path.read_bytes()[:-5000])

    with pytest.raises(PanelArchiveError) as error:
        PanelArchiveReader(PASSWORD).check(archive.path)
    assert error.value.code == PanelArchiveErrorCode.DAMAGED


def test_a_changed_byte_is_damage_whatever_the_password(archive_settings: Path) -> None:
    archive, _ = folder().create(PASSWORD, FakeDump(os.urandom(200_000)))
    raw = bytearray(archive.path.read_bytes())
    raw[100_000] ^= 0x80
    archive.path.write_bytes(bytes(raw))

    with pytest.raises(PanelArchiveError) as error:
        PanelArchiveReader(PASSWORD).check(archive.path)
    assert error.value.code == PanelArchiveErrorCode.DAMAGED
    with pytest.raises(PanelArchiveError) as error:
        PanelArchiveReader("another password").check(archive.path)
    assert error.value.code == PanelArchiveErrorCode.WRONG_PASSWORD


def test_an_archive_of_format_1_is_still_checked(archive_settings: Path) -> None:
    archive, _ = folder().create(PASSWORD, FakeDump())
    downgrade(archive.path, PASSWORD)

    assert PanelArchiveReader(PASSWORD).check(archive.path).panel_version

    archive.path.write_bytes(archive.path.read_bytes()[:-100])
    with pytest.raises(PanelArchiveError) as error:
        PanelArchiveReader(PASSWORD).check(archive.path)
    assert error.value.code == PanelArchiveErrorCode.UNREADABLE


def test_env_keeps_the_panel_variables_quoted_for_compose() -> None:
    environ = {
        "LAMNEZIA_DOMAIN": "panel.example.com",
        "LAMNEZIA_SITE_NAME": "My panel",
        "LAMNEZIA_SECRET_KEY": "a$b'c",
        "LAMNEZIA_DATABASE_URL": "postgres://lamnezia:password@postgres/lamnezia",
        "COMPOSE_PROFILES": "caddy",
        "PATH": "/usr/bin",
    }

    assert env_file(environ).splitlines() == [
        "COMPOSE_PROFILES=caddy",
        "LAMNEZIA_DOMAIN=panel.example.com",
        'LAMNEZIA_SECRET_KEY="a$$b\'c"',
        "LAMNEZIA_SITE_NAME='My panel'",
    ]


def test_the_folder_keeps_the_newest_archives(archive_settings: Path) -> None:
    root = archive_settings / "archives"
    root.mkdir()
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        (root / f"lamnezia-{stamp}.lamnezia").write_bytes(b"old")
    (root / "notes.txt").write_text("kept")

    archive, removed = folder(keep=2).create(PASSWORD, FakeDump())

    assert removed == 2
    assert [a.name for a in folder().archives()] == [
        archive.name,
        "lamnezia-20260103-000000.lamnezia",
    ]
    assert (root / "notes.txt").exists()


def test_leftovers_of_a_dead_worker_are_removed(archive_settings: Path) -> None:
    root = archive_settings / "archives"
    (root / ".dump-abc").mkdir(parents=True)
    (root / ".dump-abc" / "database.dump").write_bytes(DUMP)
    os.utime(root / ".dump-abc", (0, 0))

    folder().create(PASSWORD, FakeDump())

    assert not (root / ".dump-abc").exists()


@pytest.mark.parametrize("name", ["../db.sqlite3", "notes.txt", "lamnezia-1.lamnezia"])
def test_only_archive_names_are_served(archive_settings: Path, name: str) -> None:
    with pytest.raises(ArchiveNotFoundError):
        folder().get(name)


def test_the_dump_needs_postgres() -> None:
    with pytest.raises(PanelArchiveError) as error:
        PostgresDump({"ENGINE": "django.db.backends.sqlite3"})
    assert error.value.code == PanelArchiveErrorCode.NOT_POSTGRES
