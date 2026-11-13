"""Docker side of an Amnezia container, as upstream InstallController drives it.

Shared by the installers, the lifecycle actions and the change of the protocol settings.
"""

import io
import shlex
import tarfile
from enum import StrEnum
from pathlib import Path

from django.utils.translation import gettext as _

from amnezia_core.script_vars import container_variables, substitute
from apps.operations.services.context import OperationContext
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.host_scripts import AMNEZIA_NETWORK
from apps.protocols.services.host_setup import log_tail
from apps.ssh.services import RemoteHost

_UPSTREAM = Path(__file__).resolve().parent.parent / "upstream"
_BUILD_TIMEOUT = 900
_PULL_LIMIT_MARKER = "pull rate limit"
_PORT_IN_USE_MARKERS = (
    "address already in use",
    "port is already allocated",
    "is already in use by container",
)
# Printed by the volume part of the removal script when docker kept the volume.
_VOLUME_LEFT = "amnezia_volume_left="
# Upstream InstallController::buildRemoveContainerScript, for containers with a data volume.
_REMOVE_VOLUME = (
    "\nfor attempt in 1 2 3 4 5; do"
    "\n  docker volume rm -f $CONTAINER_NAME-data && break"
    "\n  sleep 1"
    "\ndone"
    f'\necho "{_VOLUME_LEFT}$(docker volume ls -q -f name=^$CONTAINER_NAME-data$ | head -1)"'
)


# Upstream turns the log off; when the administrator keeps it, docker rotates it.
NO_LOG_DRIVER = "none"
LOG_DRIVER = "json-file"
_LOG_OPTIONS = ("--log-opt", "max-size=10m", "--log-opt", "max-file=3")


def log_arguments(*, logs: bool) -> list[str]:
    """`docker run` log options: none as upstream, or a rotated log if `logs`."""
    if logs:
        return ["--log-driver", LOG_DRIVER, *_LOG_OPTIONS]
    return ["--log-driver", NO_LOG_DRIVER]


class Transport(StrEnum):
    UDP = "udp"
    TCP = "tcp"


def upstream_file(path: str) -> str:
    """A file copied from upstream server_scripts/; line ends are cleaned as upstream does."""
    return (_UPSTREAM / path).read_text(encoding="utf-8").replace("\r", "")


def upstream_host_script(name: str, container: str) -> str:
    """A shared upstream host script for a container; the panel runs it as root already."""
    return substitute(upstream_file(name), container_variables(container)).replace("sudo ", "")


class ProtocolContainer:
    """Image and container of one Amnezia container on a host."""

    def __init__(self, host: RemoteHost, ctx: OperationContext, name: str) -> None:
        self._host = host
        self._ctx = ctx
        self.name = name
        self.shell = host.container(name)

    def state(self, name: str | None = None) -> str:
        """Docker state of the container (`running`, `exited`...), empty if there is none."""
        name = name or self.name
        return self._host.docker(
            "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.State}}"
        ).strip()

    def check_port(
        self, port: int, transport: Transport, busy: SetupErrorCode = SetupErrorCode.PORT_BUSY
    ) -> None:
        """Refuse a port another program listens on, as upstream isServerPortBusy does."""
        script = f"lsof -i -P -n 2>/dev/null | grep -E ':{port}([^0-9]|$)' | grep -i {transport}"
        if transport == Transport.TCP:
            script += " | grep LISTEN"
        users = self._host.sh(script).output.strip()
        if users:
            self._ctx.log(users)
            raise SetupError(busy, f"{transport} {port}")
        self._ctx.log(
            _("%(transport)s port %(port)d is free.")
            % {"transport": transport.upper(), "port": port}
        )

    def has_image(self) -> bool:
        return self._host.sh(shlex.join(["docker", "image", "inspect", self.name])).ok

    def build(self, dockerfile: str) -> None:
        """Upload the Dockerfile and build the image, as upstream build_container.sh does."""
        folder = container_variables(self.name)["$DOCKERFILE_FOLDER"]
        self._host.write_file_atomic(f"{folder}/Dockerfile", dockerfile.encode(), mode=0o644)
        build = ["docker", "build", "--no-cache", "--pull", "-t", self.name, folder]
        result = self._host.sh(shlex.join(build), timeout=_BUILD_TIMEOUT)
        if not result.ok:
            log_tail(self._ctx, result)
            limited = _PULL_LIMIT_MARKER in result.output + result.errors
            code = (
                SetupErrorCode.DOCKER_PULL_LIMIT if limited else SetupErrorCode.IMAGE_BUILD_FAILED
            )
            raise SetupError(code, f"exit {result.exit_code}")

    def run_container(self, arguments: list[str]) -> None:
        """`docker run` with the arguments of upstream run_container.sh; there must be none yet."""
        result = self._host.sh(shlex.join(arguments))
        if result.ok:
            return
        log_tail(self._ctx, result)
        # A failed `docker run` can leave a created container that would block the retry.
        self.remove()
        busy = any(marker in result.errors for marker in _PORT_IN_USE_MARKERS)
        code = SetupErrorCode.PORT_BUSY if busy else SetupErrorCode.CONTAINER_START_FAILED
        raise SetupError(code, f"exit {result.exit_code}")

    def remove(self, name: str | None = None, *, volumes: bool = False) -> None:
        """Drop the container at once; its image stays for the next `docker run`.

        `volumes` drops its anonymous volumes too; a named volume always stays.
        """
        flags = "-fv" if volumes else "-f"
        self._host.sh(shlex.join(["docker", "rm", flags, name or self.name]))

    def remove_with_image(self, *, data_volume: bool = False) -> None:
        """Remove the container, its image and, if it has one, its data volume.

        Runs upstream remove_container.sh, which ignores failures: the image may be shared or
        gone. Only a container or a volume that is still there is an error.
        """
        script = upstream_host_script("remove_container.sh", self.name)
        if data_volume:
            script += substitute(_REMOVE_VOLUME, container_variables(self.name))
        output = self._host.sh(script).output
        if self.state():
            raise SetupError(SetupErrorCode.CONTAINER_NOT_REMOVED, self.name)
        left = output.rpartition(_VOLUME_LEFT)[2].strip() if data_volume else ""
        if left:
            raise SetupError(SetupErrorCode.DATA_VOLUME_LEFT, left)

    def connect_network(self) -> None:
        networks = self._host.docker(
            "inspect", "-f", "{{json .NetworkSettings.Networks}}", self.name
        )
        if f'"{AMNEZIA_NETWORK}"' not in networks:
            self._host.docker("network", "connect", AMNEZIA_NETWORK, self.name)

    def restart(self) -> None:
        self._host.docker("restart", self.name)

    def keeps_log(self) -> bool:
        """The container runs with a log driver docker can read back."""
        driver = self._host.docker(
            "inspect", "-f", "{{.HostConfig.LogConfig.Type}}", self.name
        ).strip()
        return driver != NO_LOG_DRIVER

    def inspect(self, template: str) -> str:
        """`docker inspect --format` of the container, running or stopped."""
        return self._host.docker("inspect", "--format", template, self.name)

    def has_file(self, path: str) -> bool:
        return self.shell.sh(f"[ -f {shlex.quote(path)} ]").ok

    def copy_file(self, path: str) -> bytes | None:
        """A file of the container, running or stopped; None if it has no such file.

        `docker cp` reads a stopped container too, so its files are checked before it starts.
        """
        found = self._host.sh(shlex.join(["docker", "cp", f"{self.name}:{path}", "-"]))
        if not found.ok:
            return None
        try:
            with tarfile.open(fileobj=io.BytesIO(found.stdout)) as archive:
                member = next((m for m in archive if m.isfile()), None)
                extracted = archive.extractfile(member) if member else None
                return extracted.read() if extracted else None
        except (tarfile.TarError, EOFError):
            return None
