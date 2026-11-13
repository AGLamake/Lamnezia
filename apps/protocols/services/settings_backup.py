"""A copy of the settings files of a running container, put back if a change of them fails.

configure_container.sh of the proxies rewrites the files of the data volume in place (Telemt
removes its config.toml first), so a run that fails half way leaves a proxy that does not start
again. The copy is a tar archive in the container itself, outside the volume: the files never
leave the server, the secret of the proxy included.
"""

import posixpath
import shlex
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager

from django.utils.translation import gettext as _

from apps.operations.services.context import OperationContext
from apps.ssh.services.remote import ShellTarget

ARCHIVE_PATH = "/tmp/lamnezia-settings.tar"


class SettingsBackup:
    """Folders and files of a container: saved before a change, put back as they were."""

    def __init__(
        self,
        shell: ShellTarget,
        ctx: OperationContext,
        folders: Iterable[str],
        files: Iterable[str],
    ) -> None:
        self._shell = shell
        self._ctx = ctx
        self._folders = tuple(folders)
        self._paths = (*self._folders, *files)

    @contextmanager
    def kept(self, restart: Callable[[], object]) -> Iterator[None]:
        """Save the files; if the body fails, put them back and `restart` the container."""
        with self._ctx.step(_("Save the settings files")):
            self.save()
        try:
            yield
        except Exception:
            with self._ctx.step(_("Bring back the settings files")):
                self.restore()
                restart()
                self._ctx.log(_("The service runs with the old settings again."))
            raise
        finally:
            self._shell.sh(f"rm -f -- {ARCHIVE_PATH}")

    def save(self) -> None:
        """The paths that exist go to the archive, relative to `/`."""
        names = " ".join(shlex.quote(posixpath.relpath(path, "/")) for path in self._paths)
        self._shell.run(
            f"cd / && set -- && for path in {names}; do "
            '[ -e "$path" ] && set -- "$@" "$path"; done; '
            f'tar -cf {ARCHIVE_PATH} "$@"'
        )

    def restore(self) -> None:
        """The folders lose what the change added; the archive gives back the rest."""
        clear = "".join(
            f"find {shlex.quote(folder)} -mindepth 1 -delete; " for folder in self._folders
        )
        self._shell.run(f"{clear}tar -xf {ARCHIVE_PATH} -C /")
