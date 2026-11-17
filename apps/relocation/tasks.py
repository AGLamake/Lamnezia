"""Base class of every Celery task of the panel: jobs pause while the panel moves."""

from typing import Any

from celery import Task


class PanelTask(Task):  # type: ignore[type-arg]
    """Skips the job while the panel is frozen by a move (services/freeze.py)."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # Imported here: Celery resolves the task class before Django has loaded the apps.
        from apps.relocation.services.freeze import TaskGate  # noqa: PLC0415

        if not TaskGate().allows(self.name, args):
            return None
        return super().__call__(*args, **kwargs)
