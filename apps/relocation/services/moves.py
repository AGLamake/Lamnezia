"""Starting, confirming and cancelling a panel move from the panel: checks, operations, journal."""

from django.db import transaction

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.core.models import SiteSettings
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch
from apps.relocation.errors import MoveError, MoveErrorCode
from apps.relocation.models import MoveState, PanelMove
from apps.relocation.services.freeze import MOVE_KIND_PREFIX, frozen_move
from apps.servers.models import Server

PREPARE_KIND = "relocation.prepare"
CONFIRM_KIND = "relocation.confirm"
CANCEL_KIND = "relocation.cancel"
ABANDON_ACTION = "relocation.abandon"
MOVE_PARAM = "move_id"
_ACTIVE = (OperationStatus.PENDING, OperationStatus.RUNNING)


def start_move(target: Server, user: User, ip: str | None, *, auto_stop: bool = True) -> PanelMove:
    """Freeze the panel and queue the preparation of the copy on `target`.

    With `auto_stop` this copy stops itself once the domain points to `target`.
    """
    with transaction.atomic():
        # The settings row is the panel-wide lock: two admins never start two moves.
        SiteSettings.load()
        SiteSettings.objects.select_for_update().get(pk=1)
        if frozen_move() is not None:
            raise MoveError(MoveErrorCode.WRONG_STATE)
        if Operation.objects.filter(status__in=_ACTIVE).exists():
            raise MoveError(MoveErrorCode.BUSY)
        move = PanelMove.objects.create(
            target=target, target_host=target.host, auto_stop=auto_stop, created_by=user
        )
        _launch(move, PREPARE_KIND, user)
        data = {"host": target.host, "auto_stop": auto_stop}
        audit.log(PREPARE_KIND, actor=user, target=target, data=data, ip=ip)
    return move


def confirm_move(move_id: int, user: User, ip: str | None) -> PanelMove:
    """Queue the hand-over to the copy; after it this panel stays read-only."""
    with transaction.atomic():
        move = _locked(move_id)
        if not move.can_confirm:
            raise MoveError(MoveErrorCode.WRONG_STATE, move.state)
        move.confirm_started = True
        _launch(move, CONFIRM_KIND, user)
        audit.log(CONFIRM_KIND, actor=user, target=move.target, ip=ip)
    return move


def cancel_move(move_id: int, user: User, ip: str | None) -> PanelMove:
    """Queue the removal of the copy; this panel accepts changes again."""
    with transaction.atomic():
        move = _locked(move_id)
        if not move.can_cancel:
            raise MoveError(MoveErrorCode.WRONG_STATE, move.state)
        _launch(move, CANCEL_KIND, user)
        audit.log(CANCEL_KIND, actor=user, target=move.target, ip=ip)
    return move


def abandon_move(move_id: int, user: User, ip: str | None) -> PanelMove:
    """End a partly confirmed move without the new machine; this panel accepts changes again.

    Nothing is run there: the copy may already accept changes, so the admin removes it by hand.
    """
    with transaction.atomic():
        move = _locked(move_id)
        if not move.can_abandon:
            raise MoveError(MoveErrorCode.WRONG_STATE, move.state)
        move.mark(MoveState.CANCELLED)
        audit.log(ABANDON_ACTION, actor=user, target=move.target, ip=ip)
    return move


def move_operations(move: PanelMove) -> list[Operation]:
    """The operations of a move in order, retries included, each with its steps."""
    operations = Operation.objects.filter(
        kind__startswith=MOVE_KIND_PREFIX, params__move_id=move.pk
    )
    return list(operations.order_by("pk").prefetch_related("steps"))


def _locked(move_id: int) -> PanelMove:
    moves = PanelMove.objects.select_for_update(of=("self",)).select_related("operation")
    return moves.get(pk=move_id)


def _launch(move: PanelMove, kind: str, user: User) -> None:
    move.operation = launch(kind, {MOVE_PARAM: move.pk}, user=user)
    move.save(update_fields=["operation", "confirm_started", "updated_at"])
