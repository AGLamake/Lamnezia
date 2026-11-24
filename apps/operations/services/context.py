"""What an operation handler gets: its parameters, named steps and a log per step."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from django.core.management.color import no_style
from django.db import connection
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
    def operation_id(self) -> int:
        return self._operation.pk

    @property
    def params(self) -> dict[str, Any]:
        return dict(self._operation.params)

    def remember(self, name: str, value: Any) -> None:
        """Keep a parameter the operation learnt, e.g. for the page that shows its result."""
        self._operation.params[name] = value
        self._operation.save(update_fields=["params"])

    def forget(self, name: str) -> None:
        """Remove a parameter for good, e.g. a secret the operation needs only once."""
        self._operation.params.pop(name, None)
        self._operation.save(update_fields=["params"])

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

    @contextmanager
    def database_replaced(self) -> Iterator[None]:
        """Run a block that replaces the whole database, e.g. with a restored panel archive.

        The operation and its steps lived in the old database: after the block they are saved
        in the new one, under the same number when it is free there, and the author is matched
        by the username. Nothing may be logged inside the block.
        """
        operation = self._operation
        steps = [s for s in operation.steps.order_by("position") if s != self._current]
        author = operation.created_by
        yield
        operation.created_by = (
            User.objects.filter(username=author.username).first() if author else None
        )
        operation.retry_of = None
        # The dump may hold this very operation as it was: the row is replaced by the live one.
        Operation.objects.filter(
            pk=operation.pk, kind=operation.kind, created_at=operation.created_at
        ).delete()
        if Operation.objects.filter(pk=operation.pk).exists():
            operation.pk = None
        operation.save(force_insert=True)
        Operation.objects.filter(pk=operation.pk).update(created_at=operation.created_at)
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), [Operation]):
                cursor.execute(sql)
        for step in [*steps, *([self._current] if self._current else [])]:
            step.pk = None
            step.operation = operation
            step.save(force_insert=True)

    @staticmethod
    def _finish(step: OperationStep, status: StepStatus) -> None:
        step.status = status
        step.finished_at = timezone.now()
        step.save(update_fields=["status", "finished_at"])
