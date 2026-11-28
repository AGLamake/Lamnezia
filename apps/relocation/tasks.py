"""Base class of every Celery task of the panel (jobs pause while it moves) and the domain check."""

from typing import Any

from celery import Task, shared_task

CHECK_DOMAIN_TASK = "relocation.check_domain"
FAIL_INTERRUPTED_TASK = "relocation.fail_interrupted"


class PanelTask(Task):  # type: ignore[type-arg]
    """Skips the job while the panel is frozen by a move (services/freeze.py)."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # Imported here: Celery resolves the task class before Django has loaded the apps.
        from apps.relocation.services.freeze import TaskGate  # noqa: PLC0415

        if not TaskGate().allows(self.name, args):
            return None
        return super().__call__(*args, **kwargs)


@shared_task(name=CHECK_DOMAIN_TASK)
def check_domain() -> str | None:
    """Whether the panel domain points to the new machine of a handed-over move yet."""
    from apps.relocation.services.domain import DomainCheck  # noqa: PLC0415

    move = DomainCheck.current().run()
    return move.domain_status if move else None


@shared_task(name=FAIL_INTERRUPTED_TASK)
def fail_interrupted() -> int | None:
    """Fail the move operation whose worker died or never took it; the id of that move, if any."""
    from django.utils import timezone  # noqa: PLC0415

    from apps.relocation.services.heartbeat import (  # noqa: PLC0415
        fail_interrupted_move,
        fail_unstarted_move,
    )

    move = fail_interrupted_move() or fail_unstarted_move(timezone.now())
    return move.pk if move else None
