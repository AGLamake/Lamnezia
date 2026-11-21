"""Stopping the old copy of a moved panel once its domain points to the new machine.

The panel has no access to Docker. It leaves a request file in the `archives` volume, and the
timer `lamnezia-stop` of install.sh on the host stops the stack when it finds the file. A request
nobody takes means the host has no timer.
"""

from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import get_language
from django.utils.translation import gettext as _

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.services.ports import WIKI_URL
from apps.relocation.errors import MoveError, MoveErrorCode
from apps.relocation.models import PanelMove
from apps.relocation.services.moves import MOVE_PARAM
from apps.relocation.services.target import TargetMachine
from apps.servers.services.connection import connect

STOP_KIND = "relocation.stop"
AUTO_STOP_ACTION = "relocation.auto_stop"
# The name install.sh looks for in the volume (/app/archives inside the web container).
STOP_REQUEST = "stop-after-move"
# The timer looks once a minute: a request older than this was never taken.
STOP_TIMER_GRACE = timedelta(minutes=5)


def stop_request_path() -> Path:
    return Path(settings.PANEL_ARCHIVE_DIR) / STOP_REQUEST


def stop_pending(move: PanelMove) -> bool:
    """The stop is requested and the host has not taken the request yet."""
    return move.stop_requested_at is not None and stop_request_path().exists()


def stop_unanswered(move: PanelMove) -> bool:
    """The request has waited longer than the timer ever takes."""
    return (
        stop_pending(move)
        and move.stop_requested_at is not None
        and timezone.now() - move.stop_requested_at > STOP_TIMER_GRACE
    )


def stop_help_url() -> str:
    """The wiki section on the automatic stop, in the language of the page."""
    if (get_language() or "").startswith("ru"):
        return f"{WIKI_URL}/ru-Panel-move#автоостановка-старой-копии"
    return f"{WIKI_URL}/Panel-move#automatic-stop-of-the-old-copy"


def request_stop(move: PanelMove) -> Operation | None:
    """Queue the stop once the move wants it; a stop that failed is retried from the journal."""
    if not move.should_stop:
        return None
    if Operation.objects.filter(kind=STOP_KIND, params__move_id=move.pk).exists():
        return None
    move.operation = launch(STOP_KIND, {MOVE_PARAM: move.pk}, user=move.created_by)
    move.save(update_fields=["operation", "updated_at"])
    return move.operation


def switch_auto_stop(move_id: int, enabled: bool, user: User, ip: str | None) -> PanelMove:
    """Turn the stop of the old copy on or off while it has not been requested yet."""
    with transaction.atomic():
        move = PanelMove.objects.select_for_update().get(pk=move_id)
        if not move.can_switch_auto_stop:
            raise MoveError(MoveErrorCode.WRONG_STATE, move.state)
        move.auto_stop = enabled
        move.save(update_fields=["auto_stop", "updated_at"])
        audit.log(AUTO_STOP_ACTION, actor=user, data={"enabled": enabled}, ip=ip)
    return move


class OldCopyShutdown:
    """The operation: the copy on the new machine answers, so this copy asks its host to stop."""

    def __init__(self, move: PanelMove, ctx: OperationContext) -> None:
        self._move = move
        self._ctx = ctx

    def run(self) -> None:
        if not self._move.should_stop or self._move.target is None:
            raise MoveError(MoveErrorCode.WRONG_STATE, self._move.state)
        with (
            connect(self._move.target) as host,
            self._ctx.step(_("Check the copy on the new machine")),
        ):
            TargetMachine(host, self._ctx, self._move.pk).verify_live()
        with self._ctx.step(_("Stop this copy")):
            path = stop_request_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{self._move.pk}\n")
            self._move.stop_requested_at = timezone.now()
            self._move.save(update_fields=["stop_requested_at", "updated_at"])
            self._ctx.log(
                _(
                    "The host stops this copy within a minute. If it keeps running, stop it "
                    "by hand: cd /opt/lamnezia && docker compose down."
                )
            )
