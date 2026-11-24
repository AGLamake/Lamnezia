"""Read-only panel during a move: what web requests and background jobs may still do.

While the panel is frozen, its database must not change in ways a move would lose: the archive
for the new machine is taken at the start, and later changes would stay behind on the old one.
"""

import logging
from collections.abc import Sequence
from typing import Any

from django.utils.translation import gettext as _

from apps.backups.services.panel_restore import panel_restoring
from apps.operations.models import Operation, OperationStatus
from apps.relocation.models import PanelMove
from apps.relocation.tasks import CHECK_DOMAIN_TASK

logger = logging.getLogger(__name__)

MOVE_KIND_PREFIX = "relocation."
RUN_OPERATION_TASK = "operations.run"
# Jobs that only keep the journal truthful: a stuck move operation must end, and the old panel
# reports where the domain points while it waits read-only for the switch and tells the staff
# once it has switched.
ALWAYS_ALLOWED_TASKS = frozenset(
    {"operations.fail_stale", CHECK_DOMAIN_TASK, "notifications.deliver"}
)
MOVING_ERROR = "panel_moving"
RESTORING_ERROR = "panel_restoring"


def frozen_move() -> PanelMove | None:
    """The move that keeps the panel read-only now, if any."""
    move = PanelMove.latest()
    return move if move is not None and move.frozen else None


def panel_frozen() -> bool:
    return frozen_move() is not None


class TaskGate:
    """Decides whether a Celery job runs: while frozen only the move's own operations do."""

    def allows(self, task_name: str, args: Sequence[Any]) -> bool:
        if panel_restoring():
            return self._during_restore(task_name, args)
        if task_name in ALWAYS_ALLOWED_TASKS or not panel_frozen():
            return True
        if task_name == RUN_OPERATION_TASK and args:
            return self._allows_operation(args[0])
        logger.info("task %s skipped: the panel is moving", task_name)
        return False

    @staticmethod
    def _during_restore(task_name: str, args: Sequence[Any]) -> bool:
        """Nothing runs while a panel archive replaces the database; operations fail at once."""
        logger.info("task %s skipped: the panel is being restored", task_name)
        if task_name == RUN_OPERATION_TASK and args:
            operation = Operation.objects.filter(pk=args[0]).first()
            if operation is not None and operation.status == OperationStatus.PENDING:
                operation.finish(
                    OperationStatus.FAILED,
                    code=RESTORING_ERROR,
                    message=_("The panel is being restored from an archive."),
                    hint=_("Run it again when the restore is over."),
                )
        return False

    @staticmethod
    def _allows_operation(operation_id: int) -> bool:
        operation = Operation.objects.filter(pk=operation_id).first()
        if operation is None or operation.kind.startswith(MOVE_KIND_PREFIX):
            return True
        refuse(operation)
        return False


def refuse(operation: Operation) -> None:
    """End an operation queued just before the freeze: it must not change servers behind a move."""
    if operation.status == OperationStatus.PENDING:
        operation.finish(
            OperationStatus.FAILED,
            code=MOVING_ERROR,
            message=_("The panel is moving to another machine and accepts no changes."),
            hint=_("Run it again when the move is over."),
        )
