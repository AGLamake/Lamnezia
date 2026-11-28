"""The first part of a panel move: archive the frozen panel and bring it up on the new machine.

The archive gets a random password that lives only in this process: it reaches the new machine
through the installer's stdin, and the archive is deleted there and here when the step ends.
"""

import os
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.template.defaultfilters import filesizeformat
from django.utils.translation import gettext as _

from apps.backups.services.database_dump import PostgresDump
from apps.backups.services.panel_archive import PanelArchiveWriter
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.relocation.errors import MoveError, MoveErrorCode
from apps.relocation.models import MoveState, PanelMove
from apps.relocation.services.bundle import ready_image, write_code_bundle
from apps.relocation.services.freeze import MOVE_KIND_PREFIX, refuse
from apps.relocation.services.heartbeat import MoveFlag, PreparationHeartbeat
from apps.relocation.services.relay_guard import RelayGuard
from apps.relocation.services.target import TargetMachine
from apps.servers.services.connection import connect
from apps.ssh.errors import SshError

ArchiveWriter = Callable[[str, Path], None]
# The archive folder cleans up `.staging-` files a dead worker left (panel_archives.py).
_STAGING = ".staging-move-{pk}{suffix}"


def write_panel_archive(password: str, path: Path) -> None:
    PanelArchiveWriter(
        password, PostgresDump.from_settings(), os.environ, settings.MEDIA_ROOT
    ).write(path)
    path.chmod(0o600)


class MovePreparation:
    """Checks the new machine, copies the panel there, installs and checks the copy."""

    def __init__(
        self,
        move: PanelMove,
        ctx: OperationContext,
        write_archive: ArchiveWriter = write_panel_archive,
    ) -> None:
        self._move = move
        self._ctx = ctx
        self._write_archive = write_archive
        folder = settings.PANEL_ARCHIVE_DIR
        self._archive = folder / _STAGING.format(pk=move.pk, suffix=".lamnezia")
        self._bundle = folder / _STAGING.format(pk=move.pk, suffix=".tar.gz")
        self._copying = False
        self._flag = MoveFlag.from_settings()
        self._heartbeat = PreparationHeartbeat(move, self._flag)

    def run(self) -> None:
        if self._move.state not in PanelMove.PREPARATION or self._move.target is None:
            raise MoveError(MoveErrorCode.WRONG_STATE, self._move.state)
        self._archive.parent.mkdir(parents=True, exist_ok=True)
        heartbeat = settings.RELOCATION_HEARTBEAT_TIMEOUT
        try:
            with self._flag.held(self._move.pk, heartbeat), connect(self._move.target) as host:
                machine = TargetMachine(host, self._ctx, self._move.pk, self._heartbeat.beat)
                try:
                    self._prepare(machine, RelayGuard(host, self._ctx))
                except Exception:
                    if self._copying:
                        self._discard(machine)
                    raise
        except Exception:
            # A move failed for silence may have been cancelled since: its state stays.
            self._move.mark_from(MoveState(self._move.state), MoveState.FAILED)
            raise
        finally:
            self._archive.unlink(missing_ok=True)
            self._bundle.unlink(missing_ok=True)
        if not self._move.mark_from(MoveState.PREPARING, MoveState.READY):
            raise MoveError(MoveErrorCode.INTERRUPTED)

    def _prepare(self, machine: TargetMachine, relay: RelayGuard) -> None:
        password = secrets.token_urlsafe(32)
        with self._step(_("Check the new machine")):
            machine.check()
            relay.remember()
        with self._step(_("Wait for the changes already under way")):
            self._freeze()
            self._settle()
        with self._step(_("Save the panel into an archive")), self._heartbeat.kept():
            self._write_archive(password, self._archive)
            size = filesizeformat(self._archive.stat().st_size)
            self._ctx.log(_("Archive of %(size)s.") % {"size": size})
        with self._step(_("Copy the panel to the new machine")):
            write_code_bundle(settings.BASE_DIR, self._bundle)
            self._copying = True
            machine.upload_code(self._bundle)
            remote_archive = machine.upload_archive(self._archive)
        with self._step(_("Install the panel there and restore the archive")):
            try:
                machine.install(remote_archive, password, ready_image())
            finally:
                machine.remove_uploads()
        with self._step(_("Check the copy on the new machine")):
            machine.verify(MoveState.PREPARING)
            relay.check()
        self._heartbeat.beat()

    @contextmanager
    def _step(self, title: str) -> Iterator[None]:
        """A step of the journal that starts with a heartbeat."""
        with self._ctx.step(title):
            self._heartbeat.beat()
            yield

    def _freeze(self) -> None:
        """The new machine fits: from here the panel accepts no changes."""
        if self._move.state != MoveState.CHECKING:
            return
        if not self._move.mark_from(MoveState.CHECKING, MoveState.PREPARING):
            raise MoveError(MoveErrorCode.INTERRUPTED)
        self._ctx.log(_("The new machine fits the panel: the panel is read-only from now."))

    def _settle(self) -> None:
        """Operations queued before the freeze end here; a running one stops the move."""
        time.sleep(settings.RELOCATION_SETTLE_SECONDS)
        others = Operation.objects.filter(
            status__in=(OperationStatus.PENDING, OperationStatus.RUNNING)
        ).exclude(kind__startswith=MOVE_KIND_PREFIX)
        for operation in others.filter(status=OperationStatus.PENDING):
            refuse(operation)
        running = others.filter(status=OperationStatus.RUNNING).count()
        if running:
            raise MoveError(MoveErrorCode.BUSY, f"{running} running")
        self._ctx.log(_("No other operation runs; the panel accepts no changes."))

    def _discard(self, machine: TargetMachine) -> None:
        """Take back what the failed move installed, if anything; the failure itself stays."""
        with self._ctx.step(_("Remove the copy from the new machine")):
            try:
                machine.discard()
                machine.remove_uploads()
            except (MoveError, SshError) as exc:
                self._ctx.log(exc.message)
