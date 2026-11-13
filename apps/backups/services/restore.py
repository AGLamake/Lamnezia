"""Restoring an AmneziaWG 2.0 protocol on a new server with the keys kept in the panel.

The server gets its old private key, preshared key, port and parameters, and every client
its old address, so client configs keep working while the server address stays the same.
"""

from dataclasses import dataclass
from ipaddress import IPv4Interface, IPv4Network

from django.utils import timezone
from django.utils.translation import gettext as _

from amnezia_core.awg import AwgServerConfig, Peer
from amnezia_core.awg_backup import AwgServerFiles, interface_config
from amnezia_core.awg_install import AwgInstallSettings
from amnezia_core.awg_peers import AwgPeerRegistry
from amnezia_core.clients_table import ClientRecord, ClientsTable
from amnezia_core.containers import AWG2
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.backups.errors import RestoreError, RestoreErrorCode
from apps.backups.models import ProtocolBackup
from apps.backups.services.guard import IdentityGuard
from apps.backups.services.snapshot import save_backup
from apps.keys.errors import explained_format_errors
from apps.keys.models import DISABLED_STATUSES, AccessKey, KeyStatus
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.models import ProtocolInstance, ServiceInstance
from apps.protocols.services.awg_installer import AwgInstaller
from apps.ssh.services import RemoteHost

RESTORE_KIND = "backups.restore"
RESTORE_SERVICE_KIND = "backups.restore_service"


@dataclass(frozen=True)
class RestorePlan:
    """The files to put on the server and where they came from, for the operation log."""

    files: AwgServerFiles
    backup: ProtocolBackup | None
    peers: int
    skipped: tuple[str, ...]  # active keys without an address


def plan_restore(instance: ProtocolInstance) -> RestorePlan:
    """The latest copy of the server files brought up to date with the panel keys.

    Active keys issued after the copy get their peers back, disabled and revoked ones lose
    them; clients added in the Amnezia app and kept in the copy stay. Without a copy the
    config is built from the protocol settings in the panel.
    """
    backup = ProtocolBackup.objects.filter(instance=instance).first()
    with explained_format_errors():
        if backup is not None and not _same_server(backup, instance):
            backup = None  # a copy of the server the instance had before a new import
        registry = _base_registry(instance, backup)
        skipped = _apply_keys(registry, instance)
    files = AwgServerFiles(
        registry.config.serialize(), registry.clients.serialize(), instance.preshared_key
    )
    return RestorePlan(files, backup, len(registry.config.peers), skipped)


def _same_server(backup: ProtocolBackup, instance: ProtocolInstance) -> bool:
    return _server_private_key(backup.config) == str(instance.private_key)


def _server_private_key(config: str) -> str:
    return AwgServerConfig.parse(config).private_key or ""


def _base_registry(instance: ProtocolInstance, backup: ProtocolBackup | None) -> AwgPeerRegistry:
    if backup is not None:
        return AwgPeerRegistry.parse(backup.config, backup.clients_table)
    config = interface_config(
        instance.private_key, IPv4Interface(instance.subnet), instance.port, instance.params
    )
    return AwgPeerRegistry(config, ClientsTable([]))


def _apply_keys(registry: AwgPeerRegistry, instance: ProtocolInstance) -> tuple[str, ...]:
    skipped = []
    for key in AccessKey.objects.filter(source=instance.source).order_by("pk"):
        if key.is_gone:
            registry.revoke(key.public_key)
        elif key.status in DISABLED_STATUSES:
            registry.disable(key.public_key)
        elif key.status == KeyStatus.ACTIVE and not key.client_ip:
            skipped.append(key.name)
        elif key.status == KeyStatus.ACTIVE:
            peer = Peer(key.public_key, (f"{key.client_ip}/32",), instance.preshared_key or None)
            record = ClientRecord(key.name, timezone.localtime(key.created_at), key.user_id)
            registry.enable(peer, record)
    return tuple(skipped)


class ProtocolRestorer:
    """Installs the protocol of an instance on its server again, with the saved keys.

    The server is prepared and the container built as for a new install. A container that
    already has the same server key is kept, so the operation can be run again; one with
    another key is never overwritten.
    """

    def __init__(self, instance: ProtocolInstance, host: RemoteHost, ctx: OperationContext) -> None:
        self._instance = instance
        self._host = host
        self._ctx = ctx

    def run(self) -> AwgServerConfig:
        with self._ctx.step(_("Prepare the files from the panel")):
            plan = plan_restore(self._instance)
            self._log_plan(plan)
        settings = AwgInstallSettings(
            port=self._instance.port,
            subnet=IPv4Network(self._instance.subnet),
            params=self._instance.params,
        )
        installer = AwgInstaller(
            self._host, self._ctx, settings, plan.files, logs=self._instance.container_logs
        )
        installer.guard = IdentityGuard(
            self._ctx,
            path=AWG2.config_path,
            identity=_server_private_key,
            expected=str(self._instance.private_key),
            refusal=RestoreError(RestoreErrorCode.OTHER_SERVER_KEY, self._instance.source),
        )
        config = installer.run()
        if config.private_key != self._instance.private_key:
            raise RestoreError(RestoreErrorCode.OTHER_SERVER_KEY, self._instance.source)
        if self._instance.removed:
            self._instance.container_removed_at = None
            self._instance.save(update_fields=["container_removed_at"])
        with self._ctx.step(_("Save a copy of the server files")):
            save_backup(self._instance, self._host)
        return config

    def _log_plan(self, plan: RestorePlan) -> None:
        if plan.backup is None:
            self._ctx.log(_("There is no copy of the server files: the panel settings are used."))
        else:
            taken_at = f"{timezone.localtime(plan.backup.taken_at):%Y-%m-%d %H:%M}"
            self._ctx.log(_("Copy of the server files from %(date)s.") % {"date": taken_at})
        self._ctx.log(_("Clients to restore: %(count)d.") % {"count": plan.peers})
        for name in plan.skipped:
            self._ctx.log(_("The key %(name)s has no address and is skipped.") % {"name": name})


def start_restore(instance: ProtocolInstance, user: User, ip: str | None = None) -> Operation:
    params = {"instance_id": instance.pk, "server_id": instance.server_id}
    operation = launch(RESTORE_KIND, params, user=user, subject=str(instance))
    audit.log(RESTORE_KIND, actor=user, target=instance, data={"operation": operation.pk}, ip=ip)
    return operation


def start_service_restore(service: ServiceInstance, user: User, ip: str | None = None) -> Operation:
    params = {"service_id": service.pk, "server_id": service.server_id}
    operation = launch(RESTORE_SERVICE_KIND, params, user=user, subject=str(service))
    data = {"operation": operation.pk, "container": service.container}
    audit.log(RESTORE_SERVICE_KIND, actor=user, target=service.server, data=data, ip=ip)
    return operation
