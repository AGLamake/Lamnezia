"""Copies of the protocol and service files of every server, kept in the panel for a restore.

Taking a copy only reads the files, without the server lock, as the reconciliation does.
A snapshot is a copy kept for good before a change replaces the identity of a server.
"""

import logging
from collections.abc import Callable
from functools import partial

from django.utils import timezone

from amnezia_core.awg import AwgServerConfig
from amnezia_core.awg_backup import AwgServerFiles
from amnezia_core.containers import AWG2
from amnezia_core.errors import AmneziaFormatError
from apps.backups.models import ProtocolBackup, ProtocolSnapshot, ServiceBackup, SnapshotReason
from apps.backups.services.archived import archived_protocols
from apps.backups.services.archived_services import archived_services
from apps.protocols.models import ProtocolInstance, ServiceInstance
from apps.servers.models import Server
from apps.servers.services.connection import connect
from apps.ssh.errors import RemoteFileNotFoundError, SshError
from apps.ssh.services import RemoteHost, ShellTarget

logger = logging.getLogger(__name__)


def save_backup(instance: ProtocolInstance, host: RemoteHost) -> ProtocolBackup | None:
    """Copy awg0.conf and clientsTable of the instance into the panel.

    A server that now runs AmneziaWG with another key is not the one the panel keys belong to:
    its files do not replace the copy, None is returned.
    """
    container = host.container(AWG2.name)
    config = container.read_file(AWG2.config_path).decode()
    if AwgServerConfig.parse(config).private_key != instance.private_key:
        logger.warning("%s has another server key, its backup is kept", instance.source)
        return None
    try:
        clients_table = container.read_file(AWG2.clients_table_path).decode()
    except RemoteFileNotFoundError:
        clients_table = ""  # no client was ever added in the app
    backup, _created = ProtocolBackup.objects.update_or_create(
        instance=instance,
        defaults={"config": config, "clients_table": clients_table, "taken_at": timezone.now()},
    )
    return backup


def read_awg_files(host: RemoteHost) -> AwgServerFiles:
    """The amnezia-awg2 files that make the server what it is; missing optional files are empty."""
    container = host.container(AWG2.name)
    config = container.read_file(AWG2.config_path).decode()
    return AwgServerFiles(
        config,
        _optional_file(container, AWG2.clients_table_path),
        _optional_file(container, AWG2.psk_path).strip(),
    )


def _optional_file(container: ShellTarget, path: str) -> str:
    try:
        return container.read_file(path).decode()
    except RemoteFileNotFoundError:
        return ""


def take_snapshot(
    instance: ProtocolInstance, files: AwgServerFiles, reason: SnapshotReason
) -> ProtocolSnapshot:
    """Keep the files read from the server before a change replaces its identity."""
    return ProtocolSnapshot.objects.create(
        instance=instance,
        reason=reason,
        config=files.config,
        clients_table=files.clients_table,
        preshared_key=files.preshared_key,
    )


def backed_up_containers() -> tuple[str, ...]:
    """The containers the panel keeps a copy of."""
    return (AWG2.name, *archived_protocols.containers())


def restorable(instance: ProtocolInstance) -> bool:
    """AmneziaWG 2.0 comes back from the panel keys; the others only from a saved copy."""
    if instance.container == AWG2.name:
        return True
    return archived_protocols.find(instance.container) is not None and hasattr(instance, "backup")


def save_protocol_backup(instance: ProtocolInstance, host: RemoteHost) -> ProtocolBackup | None:
    """Copy the files of any protocol with a backup; None if it has none or other keys."""
    if instance.container == AWG2.name:
        return save_backup(instance, host)
    protocol = archived_protocols.find(instance.container)
    return protocol.save(instance, host) if protocol else None


def service_restorable(service: ServiceInstance) -> bool:
    return archived_services.find(service.container) is not None and hasattr(service, "backup")


def save_service_backup(service: ServiceInstance, host: RemoteHost) -> ServiceBackup | None:
    """Copy the files of a service with a backup; None if it has none or other keys."""
    archived = archived_services.find(service.container)
    return archived.save(service, host) if archived else None


def save_instance_backup(instance_id: int) -> None:
    """A fresh copy of one protocol, after its keys changed; a failure keeps the last copy."""
    instance = (
        ProtocolInstance.objects.filter(
            pk=instance_id, container__in=backed_up_containers(), container_removed_at__isnull=True
        )
        .select_related("server")
        .first()
    )
    if instance is not None:
        _save_logged(instance.source, instance.server, partial(save_protocol_backup, instance))


def save_service_copy(service_id: int) -> None:
    """A fresh copy of one service, after its secret or settings changed; a failure keeps
    the last copy.
    """
    service = (
        ServiceInstance.objects.filter(pk=service_id, container__in=archived_services.containers())
        .select_related("server")
        .first()
    )
    if service is not None:
        _save_logged(str(service), service.server, partial(save_service_backup, service))


def save_all_backups() -> None:
    """Every instance and service with a backup in turn; one not read keeps its last copy.

    A removed container has no files: its last copy is what a restore needs.
    """
    instances = ProtocolInstance.objects.filter(
        container__in=backed_up_containers(), container_removed_at__isnull=True
    ).select_related("server")
    for instance in instances:
        _save_logged(instance.source, instance.server, partial(save_protocol_backup, instance))
    services = ServiceInstance.objects.filter(
        container__in=archived_services.containers()
    ).select_related("server")
    for service in services:
        _save_logged(str(service), service.server, partial(save_service_backup, service))


def _save_logged(name: str, server: Server, save: Callable[[RemoteHost], object]) -> None:
    try:
        with connect(server) as host:
            save(host)
    except (SshError, AmneziaFormatError) as error:
        logger.warning("backup of %s failed: %s", name, type(error).__name__)
