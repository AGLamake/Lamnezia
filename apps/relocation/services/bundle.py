"""The code of the running panel packed for the new machine, and the ready image of its version."""

import os
import tarfile
from pathlib import Path, PurePosixPath

from apps.updates.services.versions import running_version

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


def ready_image() -> str | None:
    """The published image the new machine pulls instead of building: the running release.

    None when the panel already runs a ready image, which reaches the copy with the archived
    .env, and for a build between releases, which has no published image of its own.
    """
    if os.environ.get("LAMNEZIA_IMAGE"):
        return None
    version = running_version()
    return str(version) if version is not None else None


def _keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if PurePosixPath(info.name).name in _SKIPPED:
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    return info
