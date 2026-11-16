"""Putting the AmneziaWG 2.0 files of a snapshot back on the server it was taken from.

Runs in a background operation under the server lock (DESIGN.md 5.4): read the files, keep
a snapshot of them, write the snapshot files with awg0.conf last and restart the interface;
if it does not come up, the old files come back. The panel then reads the server key and
the clients again: keys of the snapshot connect once more. A server in a group is refused.
"""

from contextlib import ExitStack
from ipaddress import IPv4Address

from django.db import transaction
from django.utils.translation import gettext as _

from amnezia_core.awg import AwgServerConfig, Peer
from amnezia_core.awg_backup import AwgServerFiles
from amnezia_core.clients_table import ClientsTable
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.backups.errors import RestoreError, RestoreErrorCode
from apps.backups.models import ProtocolSnapshot, SnapshotReason
from apps.backups.services.returned_keys import ReturnedKeys
from apps.backups.services.snapshot import read_awg_files, save_backup, take_snapshot
from apps.keys.models import GONE_STATUSES, AccessKey, KeyStatus
from apps.keys.services.adoption import adopt_peer
from apps.keys.services.service_peers import service_peers
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.awg_identity import AwgFiles, take_identity
from apps.servers.services.connection import connect
from apps.ssh.services import RemoteHost

SNAPSHOT_RESTORE_KIND = "backups.restore_snapshot"
SNAPSHOT_DELETE_KIND = "backups.delete_snapshot"


def snapshot_files(snapshot: ProtocolSnapshot) -> AwgServerFiles:
    return AwgServerFiles(snapshot.config, snapshot.clients_table, snapshot.preshared_key)


def check_standalone(instance: ProtocolInstance) -> None:
    """Raise RestoreError while the server is in a group: it must leave the group first."""
    if ProtocolInstance.objects.filter(pk=instance.pk, group_member__isnull=False).exists():
        raise RestoreError(RestoreErrorCode.IN_GROUP, instance.source)


def start_snapshot_restore(
    snapshot: ProtocolSnapshot, user: User, ip: str | None = None
) -> Operation:
    instance = snapshot.instance
    check_standalone(instance)
    params = {"snapshot_id": snapshot.pk, "server_id": instance.server_id}
    operation = launch(SNAPSHOT_RESTORE_KIND, params, user=user, subject=str(instance))
    data = {"operation": operation.pk, "snapshot": snapshot.pk}
    audit.log(SNAPSHOT_RESTORE_KIND, actor=user, target=instance, data=data, ip=ip)
    return operation


def delete_snapshot(snapshot: ProtocolSnapshot, user: User, ip: str | None = None) -> None:
    data = {"snapshot": snapshot.pk, "taken_at": snapshot.taken_at.isoformat()}
    audit.log(SNAPSHOT_DELETE_KIND, actor=user, target=snapshot.instance, data=data, ip=ip)
    snapshot.delete()


class SnapshotRestorer:
    """Gives the server of a snapshot its files back and the panel the keys they hold.

    Run again after a failure, it finds the files already in place and only saves the result.
    """

    def __init__(self, snapshot: ProtocolSnapshot, ctx: OperationContext) -> None:
        self._snapshot = snapshot
        self._instance = snapshot.instance
        self._files = snapshot_files(snapshot)
        self._ctx = ctx

    def run(self) -> None:
        with ExitStack() as stack:
            with self._ctx.step(_("Connect to the server")):
                host = stack.enter_context(connect(self._instance.server))
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(host.lock())
            check_standalone(self._instance)
            with self._ctx.step(_("Read the server files")):
                current = read_awg_files(host)
                differs = current != self._files
                if not differs:
                    self._ctx.log(_("The server already has the files of the snapshot."))
            if differs:
                self._replace(host, current)
            with self._ctx.step(_("Save the result in the panel")):
                self._save(host)

    def _replace(self, host: RemoteHost, current: AwgServerFiles) -> None:
        config = AwgServerConfig.parse(current.config)
        if config.private_key != self._instance.private_key:
            raise RestoreError(RestoreErrorCode.OTHER_SERVER_KEY, self._instance.source)
        port = AwgServerConfig.parse(self._files.config).listen_port
        if port is not None and port != config.listen_port:
            raise RestoreError(RestoreErrorCode.OTHER_PORT, str(port))
        with self._ctx.step(_("Save a snapshot of the server files")):
            take_snapshot(self._instance, current, SnapshotReason.SNAPSHOT_RESTORE)
        with self._ctx.step(_("Write the files of the snapshot")):
            AwgFiles(host, self._ctx).replace(self._files.by_path())
            if read_awg_files(host) != self._files:
                raise RestoreError(RestoreErrorCode.NOT_RESTORED, self._instance.source)

    def _save(self, host: RemoteHost) -> None:
        keys = SnapshotKeys(self._instance.source, self._files)
        with transaction.atomic():
            revoked = take_identity(self._instance, self._files)
            if revoked:
                self._ctx.log(_("Keys revoked: %(count)d.") % {"count": len(revoked)})
            back = keys.restore()
        self._ctx.log(_("Keys of the snapshot active again: %(count)d.") % {"count": back})
        if keys.elsewhere:
            self._ctx.log(
                _("Peers left out, their keys live on another server: %(count)d.")
                % {"count": keys.elsewhere}
            )
        disabled, told = ReturnedKeys(self._instance, keys.returned).settle()
        if disabled:
            self._ctx.log(_("Keys over the plan turned off: %(count)d.") % {"count": disabled})
        if told:
            self._ctx.log(_("Owners notified: %(count)d.") % {"count": told})
        save_backup(self._instance, host)


class SnapshotKeys:
    """The keys of the peers in the snapshot files: revoked or disabled ones become active,
    peers the panel never knew are taken as imported keys, as an import does.

    A peer whose key lives on another server is left out: after a group the snapshot holds the
    keys of the leading server, and a copy of them here would count twice.
    """

    def __init__(self, source: str, files: AwgServerFiles) -> None:
        self._source = source
        self._config = AwgServerConfig.parse(files.config)
        self._clients = (
            ClientsTable.parse(files.clients_table) if files.clients_table else ClientsTable([])
        )
        # Keys of users that were gone or off and are active again.
        self.returned: list[AccessKey] = []
        self.elsewhere = 0

    def restore(self) -> int:
        """Bring the peers of the snapshot back as active keys; the number of them."""
        service = service_peers.of(self._source)
        peers = [peer for peer in self._config.peers if peer.public_key not in service]
        elsewhere = self._live_elsewhere(peers)
        back = 0
        for number, peer in enumerate(peers, start=1):
            if peer.public_key in elsewhere:
                self.elsewhere += 1
                continue
            fallback = _("Imported client %(number)d") % {"number": number}
            key, created = adopt_peer(self._source, peer, self._clients, fallback)
            if not created and key.status != KeyStatus.ACTIVE and key.user_id is not None:
                self.returned.append(key)
            if not created:
                self._activate(key, peer.addresses[0] if peer.addresses else None)
            back += 1
        return back

    def _live_elsewhere(self, peers: list[Peer]) -> set[str]:
        keys = (
            AccessKey.objects.filter(public_key__in=[peer.public_key for peer in peers])
            .exclude(source=self._source)
            .exclude(status__in=GONE_STATUSES)
        )
        return set(keys.values_list("public_key", flat=True))

    @staticmethod
    def _activate(key: AccessKey, address: IPv4Address | None) -> None:
        key.status = KeyStatus.ACTIVE
        key.revoked_at = None
        if address is not None:
            key.client_ip = str(address)
        key.save(update_fields=["status", "revoked_at", "client_ip"])
