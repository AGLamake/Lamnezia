"""Failing operations whose worker died or whose job never reached a worker."""

from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from apps.operations.models import Operation, OperationStatus, StepStatus

INTERRUPTED_ERROR = "interrupted"


def fail_stale_operations(timeout: timedelta) -> int:
    """Mark operations pending or running longer than `timeout` as failed; return their number."""
    cutoff = timezone.now() - timeout
    stale = Operation.objects.filter(
        Q(status=OperationStatus.PENDING, created_at__lt=cutoff)
        | Q(status=OperationStatus.RUNNING, started_at__lt=cutoff)
    ).values_list("pk", flat=True)
    return sum(_fail_if_unfinished(pk) for pk in stale)


def _fail_if_unfinished(pk: int) -> bool:
    with transaction.atomic():
        operation = Operation.objects.select_for_update().get(pk=pk)
        if operation.is_finished:
            return False
        operation.steps.filter(status=StepStatus.RUNNING).update(
            status=StepStatus.FAILED, finished_at=timezone.now()
        )
        with translation.override(operation.language):
            operation.finish(
                OperationStatus.FAILED,
                code=INTERRUPTED_ERROR,
                message=_("The operation was interrupted."),
                hint=_("The background worker stopped or is overloaded. Check it and try again."),
            )
        return True
