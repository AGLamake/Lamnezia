"""Copies taken on events: the last one before a removal, a fresh one after a change.

The keys of a protocol or the secret and settings of a service changed on the server.
"""

import logging
from typing import Any

from django.utils.translation import gettext as _

from amnezia_core.errors import AmneziaFormatError
from apps.backups.models import ProtocolBackup
from apps.backups.services.archived_services import archived_services
from apps.backups.services.freshness import request_fresh_copy, request_fresh_service_copy
from apps.backups.services.snapshot import backed_up_containers, save_protocol_backup
from apps.keys.signals import keys_written
from apps.operations.services.context import OperationContext
from apps.protocols.models import ProtocolInstance, ServiceInstance
from apps.protocols.signals import container_removing, service_changed
from apps.ssh.errors import SshError
from apps.ssh.services import RemoteHost

logger = logging.getLogger(__name__)


def on_container_removing(
    sender: object,
    instance: ProtocolInstance,
    host: RemoteHost,
    ctx: OperationContext,
    **kwargs: Any,
) -> None:
    """A container that cannot be read is still removed: the removal dialog warned about it."""
    if instance.container not in backed_up_containers():
        return
    try:
        backup = save_protocol_backup(instance, host)
    except (SshError, AmneziaFormatError) as error:
        logger.warning("backup of %s failed: %s", instance.source, type(error).__name__)
        backup = None
    if backup is not None:
        ctx.log(_("A copy of the container files is saved in the panel."))
    elif ProtocolBackup.objects.filter(instance=instance).exists():
        ctx.log(_("A new copy could not be saved: the last saved copy is kept for a restore."))
    else:
        ctx.log(_("No copy could be saved: the keys of this protocol will not work again."))


def on_keys_written(sender: object, source: str, **kwargs: Any) -> None:
    """A key issued, changed or revoked on the server gets into a copy within a minute."""
    instance_id = ProtocolInstance.pk_of(source)
    if instance_id is not None:
        request_fresh_copy(instance_id)


def on_service_changed(sender: object, service: ServiceInstance, **kwargs: Any) -> None:
    """A new secret is in a copy at once: a restore from the old one would be refused."""
    if archived_services.find(service.container) is not None:
        request_fresh_service_copy(service.pk)


def connect() -> None:
    container_removing.connect(on_container_removing, dispatch_uid="backups.container_removing")
    keys_written.connect(on_keys_written, dispatch_uid="backups.keys_written")
    service_changed.connect(on_service_changed, dispatch_uid="backups.service_changed")
