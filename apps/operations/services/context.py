"""What an operation handler gets: its parameters, named steps and a log per step."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from django.utils import timezone
from django.utils.functional import Promise

from apps.accounts.models import User
from apps.operations.models import Operation, OperationStep, StepStatus


class OperationContext:
    """Records the progress of a running operation so the UI can show it while it runs.

    Log lines are visible to admins: never write secrets (keys, passwords) into them.
    """

    def __init__(self, operation: Operation) -> None:
        self._operation = operation
        self._position = operation.steps.count()
        self._current: OperationStep | None = None

    @property
    def params(self) -> dict[str, Any]:
        return dict(self._operation.params)

    @property
    def user(self) -> User | None:
        """Who started the operation; None for a scheduled one or a deleted account."""
        return self._operation.created_by

    @contextmanager
    def step(self, title: str | Promise) -> Iterator[None]:
        """Run a block as one step; an exception marks the step failed and goes on."""
        self._position += 1
        step = OperationStep.objects.create(
            operation=self._operation, position=self._position, title=str(title)
        )
        self._current = step
        try:
            yield
        except BaseException:
            self._finish(step, StepStatus.FAILED)
            raise
        else:
            self._finish(step, StepStatus.SUCCEEDED)
        finally:
            self._current = None

    def log(self, line: str) -> None:
        if self._current is None:
            raise RuntimeError("log() is only allowed inside a step")
        self._current.log += f"{line}\n"
        self._current.save(update_fields=["log"])

    @staticmethod
    def _finish(step: OperationStep, status: StepStatus) -> None:
        step.status = status
        step.finished_at = timezone.now()
        step.save(update_fields=["status", "finished_at"])
