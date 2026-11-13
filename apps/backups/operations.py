"""Operation kinds of the backups app; registered in `BackupsConfig.ready()`."""

from contextlib import ExitStack

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from amnezia_core.containers import AWG2
from apps.backups.errors import RestoreError, RestoreErrorCode
from apps.backups.services.archive_restore import ArchiveRestorer, ServiceRestorer
from apps.backups.services.archived import archived_protocols
from apps.backups.services.archived_services import archived_services
from apps.backups.services.restore import RESTORE_KIND, RESTORE_SERVICE_KIND, ProtocolRestorer
from apps.operations.services.context import OperationContext
from apps.operations.services.registry import operations
from apps.protocols.models import ProtocolInstance, ServiceInstance
from apps.servers.services.connection import connect


@operations.register(RESTORE_KIND, gettext_lazy("Restore the protocol"))
def restore_protocol(ctx: OperationContext) -> None:
    instance = ProtocolInstance.objects.select_related("server").get(pk=ctx.params["instance_id"])
    protocol = archived_protocols.find(instance.container)
    if protocol is None and instance.container != AWG2.name:
        raise RestoreError(RestoreErrorCode.NO_BACKUP, instance.source)
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(instance.server))
        if protocol is None:
            ProtocolRestorer(instance, host, ctx).run()
        else:
            ArchiveRestorer(instance, host, ctx, protocol).run()


@operations.register(RESTORE_SERVICE_KIND, gettext_lazy("Restore the service"))
def restore_service(ctx: OperationContext) -> None:
    service = ServiceInstance.objects.select_related("server").get(pk=ctx.params["service_id"])
    archived = archived_services.find(service.container)
    if archived is None:
        raise RestoreError(RestoreErrorCode.NO_BACKUP, str(service))
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(service.server))
        ServiceRestorer(service, host, ctx, archived).run()
