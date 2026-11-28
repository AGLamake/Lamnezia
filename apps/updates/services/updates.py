"""The panel update: what is offered, whether it can start, and starting it as an operation.

The operation makes a panel archive and leaves the request for the host; the host does the rest
(apps/updates/services/host.py). One update at a time: an operation still running, a request
not yet taken or an update the host still runs holds the next, until UPDATE_TIMEOUT.
After a rolled back update the archive made before it can be restored (`rollback_archive`).
"""

import uuid
from dataclasses import dataclass
from typing import Self

from django.core.cache import BaseCache, cache
from django.db import transaction

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.backups.services.panel_archives import (
    PASSWORD_PARAM,
    ArchiveFile,
    ArchiveNotFoundError,
    PanelArchiveFolder,
)
from apps.backups.services.panel_restore import RestoreSource, start_panel_restore
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch
from apps.ssh.encryption import SecretCipher
from apps.updates.errors import UpdateError, UpdateErrorCode
from apps.updates.services.host import HostUpdater, UpdateState
from apps.updates.services.releases import PanelRelease, PanelReleases
from apps.updates.services.versions import Version, running_version

UPDATE_KIND = "updates.panel_update"
VERSION_PARAM = "version"
RUN_PARAM = "run"
# The name of the archive the operation made, kept for the restore after a rollback.
ARCHIVE_PARAM = "archive"
# Two clicks at once: the second waits out this lock and then sees the first operation.
_START_LOCK = "updates:start"
_START_LOCK_SECONDS = 30


@dataclass(frozen=True)
class UpdateOffer:
    """The running version and the release newer than it, if there is one."""

    current: Version | None
    release: PanelRelease | None


class PanelUpdate:
    """The rules of the update page; the views and the dashboard ask only this class."""

    def __init__(
        self,
        releases: PanelReleases,
        host: HostUpdater,
        current: Version | None,
        cache_backend: BaseCache = cache,
    ) -> None:
        self.releases = releases
        self.host = host
        self.current = current
        self._cache = cache_backend

    @classmethod
    def from_settings(cls) -> Self:
        return cls(PanelReleases.from_settings(), HostUpdater.from_settings(), running_version())

    def offer(self) -> UpdateOffer:
        latest = self.releases.latest()
        newer = latest if latest and self.current and latest.version > self.current else None
        return UpdateOffer(self.current, newer)

    def active_operation(self) -> Operation | None:
        return (
            Operation.objects.filter(
                kind=UPDATE_KIND, status__in=(OperationStatus.PENDING, OperationStatus.RUNNING)
            )
            .order_by("-created_at")
            .first()
        )

    def busy(self) -> bool:
        return self.active_operation() is not None or self.host.busy()

    def start(self, version: str, password: str, user: User, ip: str | None) -> Operation:
        """Queue the update to `version`, which must be the release offered now."""
        if not self.host.available:
            raise UpdateError(UpdateErrorCode.NO_HOST)
        release = self.offer().release
        if release is None or str(release.version) != version:
            raise UpdateError(UpdateErrorCode.NOT_OFFERED, version)
        if not self._cache.add(_START_LOCK, True, _START_LOCK_SECONDS):
            raise UpdateError(UpdateErrorCode.BUSY)
        try:
            return self._launch(release, password, user, ip)
        finally:
            self._cache.delete(_START_LOCK)

    def _launch(
        self, release: PanelRelease, password: str, user: User, ip: str | None
    ) -> Operation:
        if self.busy():
            raise UpdateError(UpdateErrorCode.BUSY)
        params = {
            VERSION_PARAM: str(release.version),
            RUN_PARAM: uuid.uuid4().hex,
            PASSWORD_PARAM: SecretCipher.from_settings().encrypt(password),
        }
        with transaction.atomic():
            operation = launch(UPDATE_KIND, params, user=user)
            data = {"from": str(self.current), "to": str(release.version)}
            audit.log(UPDATE_KIND, actor=user, data=data, ip=ip)
        return operation

    def rollback_archive(self) -> ArchiveFile | None:
        """The archive made before the last update, if the host rolled that update back.

        The database keeps the migrations of the release, so the archive brings back the data
        the previous version worked with. None when it was removed as an old one.
        """
        status = self.host.status()
        if status is None or status.state != UpdateState.ROLLED_BACK or not status.run:
            return None
        operation = (
            Operation.objects.filter(kind=UPDATE_KIND, params__run=status.run)
            .order_by("-created_at")
            .first()
        )
        name = operation.params.get(ARCHIVE_PARAM) if operation else None
        if not name:
            return None
        try:
            return PanelArchiveFolder.from_settings().get(name)
        except ArchiveNotFoundError:
            return None

    def restore_rollback_archive(self, password: str, user: User, ip: str | None) -> Operation:
        """Queue the restore of the archive made before the rolled back update."""
        archive = self.rollback_archive()
        if archive is None:
            raise UpdateError(UpdateErrorCode.NO_ARCHIVE)
        return start_panel_restore(
            archive.name, password, user, ip, source=RestoreSource.UPDATE_ROLLBACK
        )
