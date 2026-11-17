"""The code of the running panel packed for the new machine, so it installs the same version."""

import tarfile
from pathlib import Path, PurePosixPath

# Built or local things: the installer on the new machine builds or pulls them anew.
_SKIPPED = frozenset(
    {
        ".git",
        ".venv",
        ".env",
        ".cache",
        "__pycache__",
        "staticfiles",
        "media",
        "archives",
        "backups",
        "db.sqlite3",
    }
)


def write_code_bundle(root: Path, target: Path) -> None:
    """Pack the code under `root` into a tar.gz at `target`, without secrets and build output."""
    with tarfile.open(target, "w:gz") as tar:
        tar.add(root, arcname=".", filter=_keep)


def _keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if PurePosixPath(info.name).name in _SKIPPED:
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    return info
