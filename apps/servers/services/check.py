"""Checking that the panel can manage a server: SSH login, root access, OS, Docker, disk space."""

from django.utils.translation import gettext as _

from apps.accounts.models import User
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.services.host_scripts import OS_RELEASE_PATH
from apps.protocols.services.host_setup import OsRelease
from apps.servers.models import CheckWarning, Server, ServerFacts
from apps.ssh.services import RemoteHost

CHECK_KIND = "servers.check"

_DOCKER_VERSION = "docker --version"
# Docker keeps images in /var/lib/docker; before Docker is installed df falls back to "/".
_DISK_FREE_KB = "df -Pk /var/lib/docker / 2>/dev/null | awk 'NR == 2 {print $4}'"


class ServerCheck:
    """Looks at a connected server and reports what it found; changes nothing on it.

    Missing root access fails the check. A missing Docker, an unsupported OS or little disk
    space only become warnings: an imported server may run another OS, and Docker is installed
    together with the first protocol.
    """

    def __init__(self, host: RemoteHost, ctx: OperationContext, min_free_disk: int) -> None:
        self._host = host
        self._ctx = ctx
        self._min_free_disk = min_free_disk
        self._warnings: list[CheckWarning] = []

    def run(self) -> ServerFacts:
        self._warnings = []
        with self._ctx.step(_("Check root access")):
            self._host.check_privileges()
        with self._ctx.step(_("Check the operating system")):
            release = OsRelease.parse(self._host.read_file(OS_RELEASE_PATH).decode())
            self._ctx.log(f"{release.pretty_name}, {self._host.run('uname -sr').strip()}")
            if not release.is_supported:
                self._warn(CheckWarning.UNSUPPORTED_OS)
        with self._ctx.step(_("Check Docker")):
            docker = self._docker_version()
            if not docker:
                self._warn(CheckWarning.NO_DOCKER)
        with self._ctx.step(_("Check free disk space")):
            disk_free = self._disk_free()
            if disk_free is not None and disk_free < self._min_free_disk:
                self._warn(CheckWarning.LOW_DISK)
        return ServerFacts(release.pretty_name, docker, disk_free, tuple(self._warnings))

    def _warn(self, warning: CheckWarning) -> None:
        self._warnings.append(warning)
        self._ctx.log(str(warning.label))

    def _docker_version(self) -> str:
        result = self._host.sh(_DOCKER_VERSION)
        version = result.output.strip() if result.ok else ""
        if version:
            self._ctx.log(version)
        return version

    def _disk_free(self) -> int | None:
        output = self._host.sh(_DISK_FREE_KB).output.strip()
        if not output.isdigit():
            self._ctx.log(_("Free disk space is unknown."))
            return None
        free = int(output) * 1024
        self._ctx.log(_("%(gb).1f GB free") % {"gb": free / 1024**3})
        return free


def start_check(server: Server, user: User | None) -> Operation:
    return launch(CHECK_KIND, {"server_id": server.pk}, user=user, subject=server.name)
