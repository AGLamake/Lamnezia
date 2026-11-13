"""Runs one operation in a worker: claims it, calls its handler, records the outcome."""

import logging
from typing import Protocol, runtime_checkable

from django.utils import timezone, translation
from django.utils.translation import gettext as _

from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.registry import OperationRegistry, operations

logger = logging.getLogger(__name__)

UNEXPECTED_ERROR = "unexpected"


@runtime_checkable
class ExplainedError(Protocol):
    """An error that tells people what went wrong and what to do, e.g. `SshError`."""

    @property
    def code(self) -> str: ...

    @property
    def message(self) -> str: ...

    @property
    def hint(self) -> str: ...


class OperationRunner:
    def __init__(self, registry: OperationRegistry = operations) -> None:
        self._registry = registry

    def run(self, operation_id: int) -> None:
        """Run a pending operation. A second delivery of the same job does nothing."""
        claimed = Operation.objects.filter(pk=operation_id, status=OperationStatus.PENDING).update(
            status=OperationStatus.RUNNING, started_at=timezone.now()
        )
        if not claimed:
            logger.info("operation %s is not pending, skipped", operation_id)
            return
        operation = Operation.objects.get(pk=operation_id)
        with translation.override(operation.language):
            try:
                self._registry.get(operation.kind).handler(OperationContext(operation))
            except Exception as exc:  # every failure must end up in the journal
                self._fail(operation, exc)
            else:
                operation.finish(OperationStatus.SUCCEEDED)

    @staticmethod
    def _fail(operation: Operation, exc: Exception) -> None:
        if isinstance(exc, ExplainedError):
            logger.warning("operation %s failed: %s", operation.pk, exc.code)
            operation.finish(
                OperationStatus.FAILED, code=str(exc.code), message=exc.message, hint=exc.hint
            )
            return
        logger.exception("operation %s failed unexpectedly", operation.pk)
        operation.finish(
            OperationStatus.FAILED,
            code=UNEXPECTED_ERROR,
            message=_("Unexpected error."),
            hint=_("Try again. If the error repeats, check the worker log and report a bug."),
        )
