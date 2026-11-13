"""Deleting finished operations older than the retention term, with their steps."""

from datetime import datetime

from apps.operations.models import Operation, OperationStatus

_FINISHED = (OperationStatus.SUCCEEDED, OperationStatus.FAILED)


def prune_operations(before: datetime) -> int:
    """Delete operations that finished before `before`; return how many were deleted."""
    old = Operation.objects.filter(status__in=_FINISHED, finished_at__lt=before)
    _, deleted = old.delete()
    return deleted.get(Operation._meta.label, 0)
