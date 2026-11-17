"""Keeps a relay on the new machine of a panel move running with its ports through the install."""

from django.utils.translation import gettext as _

from apps.operations.services.context import OperationContext
from apps.relays.services.container import RelayContainer
from apps.relocation.errors import MoveError, MoveErrorCode
from apps.ssh.services import RemoteHost


class RelayGuard:
    """Remembers the relay's ports before the install and checks them after it.

    The panel's web server takes TCP 80/443 and UDP 443, which a relay never publishes
    (RESERVED_PORTS); a Docker reinstall by the installer stops containers for a moment.
    """

    def __init__(self, host: RemoteHost, ctx: OperationContext) -> None:
        self._relay = RelayContainer(host, ctx)
        self._ctx = ctx
        self._ports: set[int] | None = None

    def remember(self) -> None:
        if not self._relay.exists:
            return
        self._ports = self._relay.published_ports()
        ports = ", ".join(str(port) for port in sorted(self._ports)) or "—"
        self._ctx.log(_("A relay runs here, UDP ports: %(ports)s.") % {"ports": ports})

    def check(self) -> None:
        if self._ports is None:
            return
        if not self._relay.running:
            self._ctx.log(_("The relay container stopped; it is started again."))
            self._relay.start_again()
        if not self._relay.running or self._relay.published_ports() != self._ports:
            raise MoveError(MoveErrorCode.RELAY_BROKEN)
        self._ctx.log(_("The relay runs with its ports."))
