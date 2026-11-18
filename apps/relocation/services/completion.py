"""The end of a panel move: confirm it (the copy takes over) or cancel it (the copy goes)."""

from django.db import transaction
from django.utils.translation import gettext as _

from apps.operations.models import OperationStatus
from apps.operations.services.context import OperationContext
from apps.relocation.errors import MoveError, MoveErrorCode
from apps.relocation.models import MoveState, PanelMove
from apps.relocation.services.target import TargetMachine
from apps.servers.services.connection import connect
from apps.ssh.errors import SshError

# States of the move in the copy's database that a confirmation accepts: as archived, or
# already confirmed by an earlier attempt.
_CONFIRMABLE_COPY = frozenset({MoveState.PREPARING, MoveState.ARRIVED})
_CANCELLABLE = frozenset({MoveState.PREPARING, MoveState.READY, MoveState.FAILED})


class MoveConfirmation:
    """The copy on the new machine accepts changes; this panel stays read-only for good."""

    def __init__(self, move: PanelMove, ctx: OperationContext) -> None:
        self._move = move
        self._ctx = ctx

    def run(self) -> None:
        if self._move.state != MoveState.READY or self._move.target is None:
            raise MoveError(MoveErrorCode.WRONG_STATE, self._move.state)
        with (
            connect(self._move.target) as host,
            self._ctx.step(_("Let the copy on the new machine accept changes")),
        ):
            machine = TargetMachine(host, self._ctx, self._move.pk)
            state = machine.copy_state()
            if state not in _CONFIRMABLE_COPY:
                raise MoveError(MoveErrorCode.NOT_STARTED, f"state {state!r}")
            machine.arrive()
        with self._ctx.step(_("Leave this panel read-only")):
            self._move.mark(MoveState.LEFT)
            self._ctx.log(
                _("Point the panel domain to %(host)s.") % {"host": self._move.target_host}
            )


class MoveCancellation:
    """The copy on the new machine is removed and this panel accepts changes again.

    When the copy cannot be reached, this panel still unfreezes, unless a confirmation has
    started: then the copy may be the live panel already and the move stays as it is, waiting
    for a retried confirmation or `abandon_move`.
    """

    def __init__(self, move: PanelMove, ctx: OperationContext) -> None:
        self._move = move
        self._ctx = ctx

    def run(self) -> None:
        if self._move.state not in _CANCELLABLE:
            raise MoveError(MoveErrorCode.WRONG_STATE, self._move.state)
        with self._ctx.step(_("Remove the copy from the new machine")):
            try:
                self._discard()
            except (MoveError, SshError) as exc:
                if exc.code == MoveErrorCode.ARRIVED:
                    raise
                self._ctx.log(exc.message)
                if self._move.confirm_started:
                    raise MoveError(MoveErrorCode.PARTLY_CONFIRMED) from exc
                self._move.mark(MoveState.CANCELLED)
                raise MoveError(MoveErrorCode.NOT_DISCARDED) from exc
        self._move.mark(MoveState.CANCELLED)

    def _discard(self) -> None:
        if self._move.target is None:
            raise MoveError(MoveErrorCode.NOT_DISCARDED, "the server is deleted")
        with connect(self._move.target) as host:
            machine = TargetMachine(host, self._ctx, self._move.pk)
            if machine.copy_state() == MoveState.ARRIVED:
                raise MoveError(MoveErrorCode.ARRIVED)
            machine.discard()
            machine.remove_uploads()
            self._ctx.log(_("The copy is removed."))


def arrive_here(move_id: int) -> PanelMove:
    """Run in the copy on the new machine: the move is over, the copy accepts changes."""
    with transaction.atomic():
        move = PanelMove.objects.select_for_update().get(pk=move_id)
        if move.state == MoveState.ARRIVED:
            return move
        if move.state not in (MoveState.PREPARING, MoveState.READY):
            raise MoveError(MoveErrorCode.WRONG_STATE, move.state)
        move.mark(MoveState.ARRIVED)
        # The archive was taken while this operation ran; here it ends with the move.
        operation = move.operation
        if operation is not None and operation.status == OperationStatus.RUNNING:
            operation.finish(OperationStatus.SUCCEEDED)
    return move
