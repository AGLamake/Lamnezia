"""Replacing a running container by a new one with other `docker run` options.

Docker cannot change the published port or the log driver of a container, so a change of the
settings that needs one starts a new container under the same name. The old one waits aside,
stopped, until the new one runs, and comes back if anything fails.
"""

import posixpath
import shlex
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager

from django.utils.translation import gettext as _

from apps.operations.services.context import OperationContext
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.container import ProtocolContainer, Transport
from apps.ssh.services import RemoteHost


def previous_name(container: str) -> str:
    """The name the old container waits under."""
    return f"{container}-previous"


class ContainerSwap:
    """A new container in place of `docker`'s; the caller creates it inside `replace`."""

    def __init__(self, host: RemoteHost, ctx: OperationContext, docker: ProtocolContainer) -> None:
        self._host = host
        self._ctx = ctx
        self._docker = docker
        self.previous = previous_name(docker.name)

    @contextmanager
    def replace(
        self,
        new_ports: Iterable[tuple[int, Transport]] = (),
        *,
        restore: Callable[[], None] | None = None,
    ) -> Iterator[None]:
        """Set the old container aside and stop it; the body runs the new one.

        Only `new_ports` are checked: the old container holds its own ports. `restore` runs in
        the old container once it is back, to undo what the body changed in a shared volume.
        """
        with self._ctx.step(_("Check the port")):
            if self._docker.state(self.previous):
                raise SetupError(SetupErrorCode.PREVIOUS_CONTAINER_LEFT, self.previous)
            for port, transport in sorted(set(new_ports)):
                self._docker.check_port(port, transport)
        with self._ctx.step(_("Set the old container aside")):
            self._host.docker("rename", self._docker.name, self.previous)
        try:
            with self._ctx.step(_("Start the container on the new port")):
                self._host.docker("stop", self.previous)
                yield
        except Exception:
            self._bring_back(restore)
            raise
        with self._ctx.step(_("Remove the old container")):
            self._docker.remove(self.previous)

    def copy_from_previous(self, path: str) -> None:
        """Copy the folder `path` of the old container into the new one, on the server itself.

        The pipe gives no exit code of its first half: the caller checks the files it needs.
        """
        parent = posixpath.dirname(path)
        source = shlex.quote(f"{self.previous}:{path}")
        target = shlex.quote(f"{self._docker.name}:{parent}")
        self._host.run(f"docker cp {source} - | docker cp - {target}")

    def _bring_back(self, restore: Callable[[], None] | None) -> None:
        """The old container, with its port and files, runs again."""
        name = self._docker.name
        with self._ctx.step(_("Bring back the old container")):
            self._docker.remove()
            self._host.docker("rename", self.previous, name)
            self._host.docker("start", name)
            if restore is not None:
                restore()
