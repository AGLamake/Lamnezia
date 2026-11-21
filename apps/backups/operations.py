"""Operation kinds of the backups app; registered in `BackupsConfig.ready()`."""

from contextlib import ExitStack

from django.template.defaultfilters import filesizeformat
from django.utils import timezone
from django.utils.functional import Promise
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from amnezia_core.containers import AWG2
from apps.backups.errors import (
    PANEL_ARCHIVE_ERRORS,
    PanelArchiveError,
    PanelArchiveErrorCode,
    RestoreError,
    RestoreErrorCode,
)
from apps.backups.models import ProtocolSnapshot
from apps.backups.services.archive_remote import ArchiveRemote, remote_from_settings
from apps.backups.services.archive_restore import ArchiveRestorer, ServiceRestorer
from apps.backups.services.archive_schedule import ArchiveSchedule
from apps.backups.services.archived import archived_protocols
from apps.backups.services.archived_services import archived_services
from apps.backups.services.panel_archives import (
    PANEL_ARCHIVE_KIND,
    PASSWORD_PARAM,
    SCHEDULED_PARAM,
    ArchiveFile,
    PanelArchiveFolder,
)
from apps.backups.services.restore import RESTORE_KIND, RESTORE_SERVICE_KIND, ProtocolRestorer
from apps.backups.services.snapshot_restore import SNAPSHOT_RESTORE_KIND, SnapshotRestorer
from apps.notifications.events import Event, english
from apps.notifications.services.delivery import emit
from apps.operations.services.context import OperationContext
from apps.operations.services.registry import operations
from apps.protocols.models import ProtocolInstance, ServiceInstance
from apps.servers.services.connection import connect
from apps.ssh.encryption import SecretCipher


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


@operations.register(SNAPSHOT_RESTORE_KIND, gettext_lazy("Restore a snapshot of the protocol"))
def restore_snapshot(ctx: OperationContext) -> None:
    snapshot = ProtocolSnapshot.objects.select_related("instance__server").get(
        pk=ctx.params["snapshot_id"]
    )
    SnapshotRestorer(snapshot, ctx).run()


@operations.register(PANEL_ARCHIVE_KIND, gettext_lazy("Archive the panel"))
def archive_panel(ctx: OperationContext) -> None:
    """Make an archive and send its copy to the external storage, if one is set.

    A failure is told to the staff once a day: a scheduled archive has nobody watching it.
    """
    try:
        archive = write_panel_archive(ctx)
        # Read after the archive: wrong storage settings must not cost the archive itself.
        remote = remote_from_settings()
        if remote is not None:
            _send_copy(ctx, remote, archive)
    except Exception as exc:
        _tell_failed(exc)
        raise


def write_panel_archive(ctx: OperationContext) -> ArchiveFile:
    """The archive step, also the first step of the panel update."""
    password = _archive_password(ctx)
    with ctx.step(_("Save the database, settings and files into the archive")):
        archive, removed = PanelArchiveFolder.from_settings().create(password)
        size = filesizeformat(archive.size)
        ctx.log(_("Archive %(name)s, %(size)s.") % {"name": archive.name, "size": size})
        if removed:
            ctx.log(_("Old archives removed: %(count)d.") % {"count": removed})
    return archive


def _archive_password(ctx: OperationContext) -> str:
    if ctx.params.get(SCHEDULED_PARAM):
        return ArchiveSchedule.from_settings().checked_password()
    token = ctx.params.get(PASSWORD_PARAM)
    # Erased before the dump, so the archive and the database never keep the password.
    ctx.forget(PASSWORD_PARAM)
    if not token:
        raise PanelArchiveError(PanelArchiveErrorCode.NO_PASSWORD)
    return SecretCipher.from_settings().decrypt(token)


def _send_copy(ctx: OperationContext, remote: ArchiveRemote, archive: ArchiveFile) -> None:
    with ctx.step(_("Send a copy to %(storage)s") % {"storage": remote.label}):
        try:
            remote.send(archive.path)
        except PanelArchiveError as error:
            if error.detail:
                ctx.log(_("Answer: %(detail)s") % {"detail": error.detail})
            raise
        ctx.log(_("The copy %(name)s is in the storage.") % {"name": archive.name})


def _tell_failed(exc: Exception) -> None:
    message: str | Promise = gettext_lazy("Unexpected error.")
    if isinstance(exc, PanelArchiveError):
        message = PANEL_ARCHIVE_ERRORS[exc.code].message
    key = f"panel_archive_failed:{timezone.localdate()}"
    emit(Event.PANEL_ARCHIVE_FAILED, {"error": english(message)}, key=key)
