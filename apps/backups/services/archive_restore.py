"""Restoring a protocol or a service from the saved copy of its container folders, on its server.

The container is installed by its own installer; the saved files go into the new container
before it is configured, and the installer keeps them as it keeps the files of any server.
A container already on the server is checked before it is started; one a failed restore left
with only part of the copy is removed and made anew.
"""

from django.utils import timezone
from django.utils.translation import gettext as _

from amnezia_core.container_archive import (
    EXTRACT_COMMAND,
    FINISH_COMMAND,
    RESTORE_MARKER,
    ArchiveFormatError,
    ContainerArchive,
)
from amnezia_core.errors import AmneziaFormatError
from apps.backups.errors import RestoreError, RestoreErrorCode
from apps.backups.models import ProtocolBackup, ServiceBackup
from apps.backups.services.archived import ArchivedProtocol
from apps.backups.services.archived_services import ArchivedService
from apps.keys.models import AccessKey, KeyStatus
from apps.operations.services.context import OperationContext
from apps.protocols.models import ProtocolInstance, ServiceInstance
from apps.protocols.services.container import ProtocolContainer
from apps.ssh.services import RemoteHost


class ArchiveSeed:
    """Puts a saved copy into a new container; a container that has its files keeps them.

    Whether the kept files are those of the panel is checked before the container starts.
    `own_keys` is for a service that makes its keys itself when it starts (Tor, sshd): the
    files it made are replaced by the copy and the container is restarted to read them. A
    database dumped into the copy is loaded back once the container runs.

    The container is marked until the copy is all in: a restore that fails on the way leaves
    a container with keys of its own, which the next attempt removes instead of refusing it.
    """

    def __init__(
        self, archive: ContainerArchive, ctx: OperationContext, *, own_keys: bool = False
    ) -> None:
        self._archive = archive
        self._ctx = ctx
        self._own_keys = own_keys

    def put(self, docker: ProtocolContainer) -> None:
        with self._ctx.step(_("Put the saved files into the container")):
            if not self._own_keys and docker.has_file(self._archive.state.identity_path):
                self._ctx.log(_("The container already has its files, they are kept."))
                return
            docker.shell.run(EXTRACT_COMMAND, stdin=self._archive.data)
            self._ctx.log(_("Files put back: %(count)d.") % {"count": len(self._archive.files())})
            if self._own_keys:
                docker.restart()
        database = self._archive.state.database
        if database and database.path in self._archive.files():  # older copies have none
            with self._ctx.step(_("Load the saved database")):
                docker.shell.run(database.load_command(), timeout=database.load_timeout)
        docker.shell.run(FINISH_COMMAND)

    def unfinished(self, docker: ProtocolContainer) -> bool:
        return docker.copy_file(RESTORE_MARKER) is not None


class ArchiveRestorer:
    """Installs the protocol of an instance on its server again, from the saved copy.

    A container that already runs with the same keys is kept, so the operation can be run
    again; one with other keys is never overwritten.
    """

    def __init__(
        self,
        instance: ProtocolInstance,
        host: RemoteHost,
        ctx: OperationContext,
        protocol: ArchivedProtocol,
    ) -> None:
        self._instance = instance
        self._host = host
        self._ctx = ctx
        self._protocol = protocol

    def run(self) -> None:
        with self._ctx.step(_("Prepare the files from the panel")):
            archive = self._saved_archive()
        installer = self._protocol.installer(self._host, self._ctx, self._instance)
        installer.seed = ArchiveSeed(archive, self._ctx)
        installer.guard = self._protocol.guard(self._ctx, self._instance)
        installer.run()
        with self._ctx.step(_("Save a copy of the server files")):
            if self._protocol.save(self._instance, self._host) is None:
                raise RestoreError(RestoreErrorCode.OTHER_CONTAINER, self._instance.source)
        if self._instance.removed:
            self._instance.container_removed_at = None
            self._instance.save(update_fields=["container_removed_at"])

    def _saved_archive(self) -> ContainerArchive:
        backup = ProtocolBackup.objects.filter(instance=self._instance).first()
        if backup is None or not backup.archive:
            raise RestoreError(RestoreErrorCode.NO_BACKUP, self._instance.source)
        try:
            archive = ContainerArchive.decode(self._protocol.state, backup.archive)
            identity = archive.identity()
        except ArchiveFormatError as error:
            raise RestoreError(RestoreErrorCode.BACKUP_INVALID, str(error)) from error
        if identity != self._protocol.identity(self._instance):
            raise RestoreError(RestoreErrorCode.BACKUP_OF_OTHER_SERVER, self._instance.source)
        self._log_backup(backup)
        return archive

    def _log_backup(self, backup: ProtocolBackup) -> None:
        taken_at = f"{timezone.localtime(backup.taken_at):%Y-%m-%d %H:%M}"
        self._ctx.log(_("Copy of the server files from %(date)s.") % {"date": taken_at})
        newer = AccessKey.objects.filter(
            source=self._instance.source, status=KeyStatus.ACTIVE, created_at__gt=backup.taken_at
        ).count()
        if newer:
            self._ctx.log(
                _("Keys issued after the copy: %(count)d. They may not connect; reissue them.")
                % {"count": newer}
            )


class ServiceRestorer:
    """Installs a service on its server again from the saved copy of its container.

    As for a protocol, a container with the same keys is kept and one with other keys is
    never started or overwritten.
    """

    def __init__(
        self,
        service: ServiceInstance,
        host: RemoteHost,
        ctx: OperationContext,
        archived: ArchivedService,
    ) -> None:
        self._service = service
        self._host = host
        self._ctx = ctx
        self._archived = archived

    def run(self) -> None:
        with self._ctx.step(_("Prepare the files from the panel")):
            copy = self._saved_copy()
        installer = self._archived.installer(self._host, self._ctx, self._service, copy)
        installer.seed = ArchiveSeed(copy, self._ctx, own_keys=self._archived.makes_own_keys)
        installer.guard = self._archived.guard(self._ctx, self._service)
        installer.run()
        with self._ctx.step(_("Save a copy of the server files")):
            if self._archived.save(self._service, self._host) is None:
                raise RestoreError(RestoreErrorCode.OTHER_CONTAINER, str(self._service))

    def _saved_copy(self) -> ContainerArchive:
        backup = ServiceBackup.objects.filter(service=self._service).first()
        if backup is None:
            raise RestoreError(RestoreErrorCode.NO_BACKUP, str(self._service))
        try:
            copy = ContainerArchive.decode(self._archived.state, backup.archive)
            matches = self._archived.copy_matches(self._service, copy)
        except AmneziaFormatError as error:
            raise RestoreError(RestoreErrorCode.BACKUP_INVALID, str(error)) from error
        if not matches:
            raise RestoreError(RestoreErrorCode.BACKUP_OF_OTHER_SERVER, str(self._service))
        taken_at = f"{timezone.localtime(backup.taken_at):%Y-%m-%d %H:%M}"
        self._ctx.log(_("Copy of the server files from %(date)s.") % {"date": taken_at})
        return copy
