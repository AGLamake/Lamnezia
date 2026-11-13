"""The TSO/GSO workaround of a server: reading the offloads, turning the workaround on and off.

The scripts and units are in services/offload_scripts.py. The panel keeps the latest reading in
`OffloadWorkaround`, so the server card shows it without connecting.
"""

from contextlib import ExitStack

from django.utils import timezone
from django.utils.translation import gettext as _

from amnezia_core.containers import WG_CONTAINERS
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.models import OffloadWorkaround, ProtocolInstance
from apps.protocols.services import host_scripts
from apps.protocols.services import offload_scripts as scripts
from apps.protocols.services.host_setup import log_tail
from apps.protocols.services.offload_scripts import OffloadReading
from apps.servers.models import Server
from apps.ssh.services import RemoteHost

CHECK_KIND = "protocols.offload.check"
ENABLE_KIND = "protocols.offload.enable"
DISABLE_KIND = "protocols.offload.disable"
KINDS = (CHECK_KIND, ENABLE_KIND, DISABLE_KIND)


class OffloadFix:
    """Turns the workaround on and off under the server lock (DESIGN.md 5.4); safe to repeat.

    Every action ends with a reading of the containers, saved for the server card.
    """

    def __init__(self, server: Server, host: RemoteHost, ctx: OperationContext) -> None:
        self._server = server
        self._host = host
        self._ctx = ctx

    def check(self) -> OffloadReading:
        with self._ctx.step(_("Read TSO/GSO of the containers")):
            reading = OffloadReading.parse(self._host.run(scripts.STATUS))
            self._report(reading)
        record(self._server, reading)
        return reading

    def enable(self) -> OffloadReading:
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            self._enable()
        return self.check()

    def _enable(self) -> None:
        with self._ctx.step(_("Check systemd")):
            if not self._host.sh(scripts.HAS_SYSTEMD).ok:
                raise SetupError(SetupErrorCode.NO_SYSTEMD, "no /run/systemd/system")
        with self._ctx.step(_("Install ethtool")):
            self._install_tools()
        with self._ctx.step(_("Turn TSO/GSO off and keep it off")):
            for path, content, mode in scripts.FILES:
                self._host.write_file_atomic(path, content.encode(), mode)
            for line in self._host.run(scripts.ENABLE).splitlines():
                self._ctx.log(line)
            self._ctx.log(
                _("The %(unit)s timer turns them off again after a container restart.")
                % {"unit": f"{scripts.UNIT}.timer"}
            )

    def disable(self) -> OffloadReading:
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Remove the workaround and turn TSO/GSO back on")):
                self._host.run(scripts.DISABLE)
        return self.check()

    def _install_tools(self) -> None:
        missing = self._host.run(scripts.MISSING_COMMANDS).split()
        if not missing:
            self._ctx.log(_("Everything is already installed."))
            return
        packages = scripts.install_list(missing)
        self._ctx.log(_("Installing: %(packages)s") % {"packages": " ".join(packages)})
        result = self._host.sh(
            host_scripts.install_packages(packages), timeout=host_scripts.APT_TIMEOUT
        )
        if not result.ok:
            log_tail(self._ctx, result)
            raise SetupError(SetupErrorCode.OFFLOAD_TOOLS_FAILED, f"exit {result.exit_code}")

    def _report(self, reading: OffloadReading) -> None:
        # Whole sentences: a bare "on" is translated elsewhere for another noun.
        self._ctx.log(_("Workaround: on.") if reading.timer_active else _("Workaround: off."))
        if not reading.interfaces:
            self._ctx.log(_("No running AmneziaWG or WireGuard container."))
        for interface in reading.interfaces:
            self._ctx.log(
                f"{interface.container} {interface.interface}: "
                f"TSO {interface.tso}, GSO {interface.gso}"
            )
        if any(not interface.known for interface in reading.interfaces):
            self._ctx.log(_("ethtool is not installed: turning the workaround on installs it."))


def record(server: Server, reading: OffloadReading) -> OffloadWorkaround:
    workaround, _created = OffloadWorkaround.objects.update_or_create(
        server=server,
        defaults={
            "enabled": reading.timer_active,
            "interfaces": [interface.to_json() for interface in reading.interfaces],
            "checked_at": timezone.now(),
        },
    )
    return workaround


def applies(server: Server) -> bool:
    """The server has a WireGuard-family protocol whose container is on it."""
    return ProtocolInstance.objects.filter(
        server=server, container__in=WG_CONTAINERS, container_removed_at=None
    ).exists()


def start(kind: str, server: Server, user: User, *, ip: str | None = None) -> Operation:
    """Start a check, or turn the workaround on or off; the change goes to the audit journal."""
    if kind not in KINDS:
        raise ValueError(f"not an offload operation: {kind!r}")
    operation = launch(kind, {"server_id": server.pk}, user=user, subject=server.name)
    if kind != CHECK_KIND:
        audit.log(kind, actor=user, target=server, data={"operation": operation.pk}, ip=ip)
    return operation
