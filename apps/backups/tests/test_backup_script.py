import os
import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings

SCRIPT = settings.BASE_DIR / "deploy" / "backup" / "backup.sh"
# Writes its arguments as the dump, or fails as pg_dump does when the database is down.
FAKE_PG_DUMP = """#!/bin/sh
[ -n "${PG_DUMP_FAILS:-}" ] && exit 1
for arg; do case "$arg" in --file=*) echo "$@" > "${arg#--file=}";; esac; done
"""

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell")


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    paths = {name: tmp_path / name for name in ("bin", "backups", "data")}
    for path in paths.values():
        path.mkdir()
    (paths["data"] / "media").mkdir()
    (paths["data"] / "media" / "logo.png").write_bytes(b"png")
    pg_dump = paths["bin"] / "pg_dump"
    pg_dump.write_text(FAKE_PG_DUMP, newline="\n")
    pg_dump.chmod(0o755)
    return paths


def run_once(dirs: dict[str, Path], **env: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PATH": f"{dirs['bin']}{os.pathsep}{os.environ['PATH']}",
        "BACKUP_DIR": dirs["backups"].as_posix(),
        "DATA_DIR": dirs["data"].as_posix(),
        **env,
    }
    command = [shutil.which("sh") or "sh", SCRIPT.as_posix(), "once"]
    return subprocess.run(command, env=environment, capture_output=True, text=True, check=False)


def names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def test_backup_saves_the_dump_and_the_media(dirs: dict[str, Path]) -> None:
    done = run_once(dirs)

    assert done.returncode == 0, done.stderr
    dump, media = names(dirs["backups"])
    assert dump.startswith("db-") and dump.endswith(".dump")
    assert media.startswith("media-") and media.endswith(".tar.gz")
    assert "--format=custom" in (dirs["backups"] / dump).read_text()


def test_only_the_newest_backups_are_kept(dirs: dict[str, Path]) -> None:
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        (dirs["backups"] / f"db-{stamp}.dump").write_text("old")
        (dirs["backups"] / f"media-{stamp}.tar.gz").write_text("old")
    (dirs["backups"] / "notes.txt").write_text("mine")

    assert run_once(dirs, LAMNEZIA_BACKUP_KEEP="2").returncode == 0

    kept = names(dirs["backups"])
    assert "notes.txt" in kept
    assert "db-20260103-000000.dump" in kept
    assert "db-20260102-000000.dump" not in kept
    assert len([name for name in kept if name.startswith("db-")]) == 2
    assert len([name for name in kept if name.startswith("media-")]) == 2


def test_failed_dump_keeps_the_previous_backups(dirs: dict[str, Path]) -> None:
    old = dirs["backups"] / "db-20260101-000000.dump"
    old.write_text("old")

    done = run_once(dirs, PG_DUMP_FAILS="1", LAMNEZIA_BACKUP_KEEP="1")

    assert done.returncode != 0
    assert names(dirs["backups"]) == [old.name]
