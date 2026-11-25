"""Closing the cloud metadata service to the VPN clients of a server, and opening it again.

The rule and its timer are in services/metadata_scripts.py. An install closes the server on its
own (`MetadataBlocker.ensure`) and an import, which changes nothing on the server, starts an
operation that does (`close_after_import`), unless an administrator opened it on the server card.
Servers added before the block, or closed by an older script, get the same operation from an
hourly task (`catch_up`).
The panel keeps the latest reading in `MetadataBlock`, so the card shows it without connecting.
"""

from contextlib import ExitStack

from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.models import MetadataBlock, ProtocolInstance, ServiceInstance
from apps.protocols.services import metadata_scripts as scripts
from apps.protocols.services.metadata_scripts import MetadataReading
from apps.servers.models import Server
from apps.ssh.errors import CatalogError
from apps.ssh.services import RemoteHost

CHECK_KIND = "protocols.metadata.check"
ENABLE_KIND = "protocols.metadata.enable"
DISABLE_KIND = "protocols.metadata.disable"
KINDS = (CHECK_KIND, ENABLE_KIND, DISABLE_KIND)
# Closes the server after an import or for `catch_up`, unless an administrator opened it.
ENSURE_KIND = "protocols.metadata.ensure"


class MetadataBlocker:
    """Closes and opens 169.254.0.0/16 to the containers under the server lock (DESIGN.md 5.4).

    Safe to repeat; every action ends with a reading of the rule, saved for the server card.
    """

    def __init__(self, server: Server, host: RemoteHost, ctx: OperationContext) -> None:
        self._server = server
        self._host = host
        self._ctx = ctx

    def check(self) -> MetadataReading:
        with self._ctx.step(_("Check the cloud metadata block")):
            reading = MetadataReading.parse(self._host.run(scripts.STATUS))
            self._report(reading)
        record(self._server, reading)
        return reading

    def enable(self) -> MetadataReading:
        """Close the metadata service; the choice is kept for later installs and imports."""
        self._close()
        MetadataBlock.objects.filter(server=self._server).update(turned_off=False)
        return self.check()

    def disable(self) -> MetadataReading:
        """Open the metadata service; install and import then leave the server open."""
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Open the cloud metadata service to the VPN clients")):
                self._host.run(scripts.DISABLE)
        MetadataBlock.objects.update_or_create(server=self._server, defaults={"turned_off": True})
        return self.check()

    def ensure(self) -> None:
        """Close the server after an install or an import, unless an administrator opened it.

        A server it cannot close is left as it is, with the reason in the log: the install or
        the import before it has already done its work, and the server card closes it later.
        """
        if not should_close(self._server):
            return
        try:
            self._close()
            self.check()
        except CatalogError as error:
            with self._ctx.step(_("Leave the cloud metadata service open")):
                self._ctx.log(error.message)
                self._ctx.log(error.hint)

    def _close(self) -> None:
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Check systemd")):
                if not self._host.sh(scripts.HAS_SYSTEMD).ok:
                    raise SetupError(SetupErrorCode.METADATA_NO_SYSTEMD, "no /run/systemd/system")
            with self._ctx.step(_("Close the cloud metadata service to the VPN clients")):
                for path, content, mode in scripts.FILES:
                    self._host.write_file_atomic(path, content.encode(), mode)
                MetadataBlock.objects.update_or_create(
                    server=self._server, defaults={"script_version": scripts.VERSION}
                )
                for line in self._host.run(scripts.ENABLE).splitlines():
                    self._ctx.log(line)
                self._ctx.log(
                    _("The %(unit)s timer puts the rule back after a reboot.")
                    % {"unit": f"{scripts.UNIT}.timer"}
                )

    def _report(self, reading: MetadataReading) -> None:
        if reading.closed:
            self._ctx.log(_("The cloud metadata service is closed to the VPN clients."))
        else:
            self._ctx.log(_("The cloud metadata service is open to the VPN clients."))
        for bridge in reading.bridges:
            self._ctx.log(
                _("%(bridge)s: %(network)s closed.")
                % {"bridge": bridge, "network": scripts.LINK_LOCAL}
            )


def record(server: Server, reading: MetadataReading) -> MetadataBlock:
    block, _created = MetadataBlock.objects.update_or_create(
        server=server,
        defaults={
            "enabled": reading.closed,
            "bridges": list(reading.bridges),
            "checked_at": timezone.now(),
        },
    )
    return block


def should_close(server: Server) -> bool:
    """The server has an Amnezia container and an administrator did not open it on the card."""
    if MetadataBlock.objects.filter(server=server, turned_off=True).exists():
        return False
    return applies(server)


def applies(server: Server) -> bool:
    """The server has an Amnezia container: a protocol or a service."""
    protocols = ProtocolInstance.objects.filter(server=server, container_removed_at=None)
    return protocols.exists() or ServiceInstance.objects.filter(server=server).exists()


def close_after_import(server: Server, user: User | None) -> Operation | None:
    """Start closing the server after its import, if it should be closed."""
    if not should_close(server):
        return None
    return launch(ENSURE_KIND, {"server_id": server.pk}, user=user, subject=server.name)


def catch_up() -> list[Operation]:
    """Start closing the servers added before the block, or closed by an older script.

    Each server is tried once per script version: one that cannot be closed (no systemd, no
    answer) stays as it is, and the server card closes it later.
    """
    behind = Server.objects.exclude(metadata_block__script_version__gte=scripts.VERSION)
    started = []
    for server in behind.order_by("pk"):
        if not should_close(server):
            continue
        MetadataBlock.objects.update_or_create(
            server=server, defaults={"script_version": scripts.VERSION}
        )
        started.append(launch(ENSURE_KIND, {"server_id": server.pk}, subject=server.name))
    return started


def start(kind: str, server: Server, user: User, *, ip: str | None = None) -> Operation:
    """Start a check, or close or open the metadata service; a change goes to the audit journal."""
    if kind not in KINDS:
        raise ValueError(f"not a metadata block operation: {kind!r}")
    operation = launch(kind, {"server_id": server.pk}, user=user, subject=server.name)
    if kind != CHECK_KIND:
        audit.log(kind, actor=user, target=server, data={"operation": operation.pk}, ip=ip)
    return operation
