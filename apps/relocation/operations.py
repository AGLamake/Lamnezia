"""Operation kinds of the panel move; registered in `RelocationConfig.ready()`."""

from django.conf import settings
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.operations.services.context import OperationContext
from apps.operations.services.registry import operations
from apps.relocation.models import PanelMove
from apps.relocation.services.completion import MoveCancellation, MoveConfirmation
from apps.relocation.services.heartbeat import MoveFlag
from apps.relocation.services.moves import (
    CANCEL_KIND,
    CHECK_KIND,
    CONFIRM_KIND,
    MOVE_PARAM,
    PREPARE_KIND,
    SERVER_PARAM,
)
from apps.relocation.services.preparation import MovePreparation
from apps.relocation.services.shutdown import STOP_KIND, OldCopyShutdown
from apps.relocation.services.target import TargetMachine
from apps.servers.models import Server
from apps.servers.services.connection import connect


def _move(ctx: OperationContext) -> PanelMove:
    return PanelMove.objects.select_related("target").get(pk=ctx.params[MOVE_PARAM])


@operations.register(CHECK_KIND, gettext_lazy("Move the panel: check the new machine"))
def check_target(ctx: OperationContext) -> None:
    """The first step of a move alone, to learn whether the machine suits before freezing."""
    server = Server.objects.get(pk=ctx.params[SERVER_PARAM])
    with ctx.step(_("Check the new machine")), connect(server) as host:
        TargetMachine(host, ctx, move_id=0).check(only_look=True)
        ctx.log(_("The machine suits the move."))


@operations.register(PREPARE_KIND, gettext_lazy("Move the panel: prepare the new machine"))
def prepare_move(ctx: OperationContext) -> None:
    MovePreparation(_move(ctx), ctx).run()


# The host does not rebuild the stack under these two, and a flag that runs out fails them
# (services/heartbeat.py): a confirmation runs short commands, a cancel removes the copy.
@operations.register(CONFIRM_KIND, gettext_lazy("Move the panel: hand over to the new machine"))
def confirm_move(ctx: OperationContext) -> None:
    move = _move(ctx)
    with MoveFlag.from_settings().held(move.pk, settings.RELOCATION_HEARTBEAT_TIMEOUT):
        MoveConfirmation(move, ctx).run()


@operations.register(CANCEL_KIND, gettext_lazy("Move the panel: cancel"))
def cancel_move(ctx: OperationContext) -> None:
    move = _move(ctx)
    with MoveFlag.from_settings().held(move.pk, settings.RELOCATION_INSTALL_TIMEOUT):
        MoveCancellation(move, ctx).run()


@operations.register(STOP_KIND, gettext_lazy("Move the panel: stop the old copy"))
def stop_old_copy(ctx: OperationContext) -> None:
    OldCopyShutdown(_move(ctx), ctx).run()
