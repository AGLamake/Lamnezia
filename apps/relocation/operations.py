"""Operation kinds of the panel move; registered in `RelocationConfig.ready()`."""

from django.utils.translation import gettext_lazy

from apps.operations.services.context import OperationContext
from apps.operations.services.registry import operations
from apps.relocation.models import PanelMove
from apps.relocation.services.completion import MoveCancellation, MoveConfirmation
from apps.relocation.services.moves import CANCEL_KIND, CONFIRM_KIND, MOVE_PARAM, PREPARE_KIND
from apps.relocation.services.preparation import MovePreparation


def _move(ctx: OperationContext) -> PanelMove:
    return PanelMove.objects.select_related("target").get(pk=ctx.params[MOVE_PARAM])


@operations.register(PREPARE_KIND, gettext_lazy("Move the panel: prepare the new machine"))
def prepare_move(ctx: OperationContext) -> None:
    MovePreparation(_move(ctx), ctx).run()


@operations.register(CONFIRM_KIND, gettext_lazy("Move the panel: hand over to the new machine"))
def confirm_move(ctx: OperationContext) -> None:
    MoveConfirmation(_move(ctx), ctx).run()


@operations.register(CANCEL_KIND, gettext_lazy("Move the panel: cancel"))
def cancel_move(ctx: OperationContext) -> None:
    MoveCancellation(_move(ctx), ctx).run()
