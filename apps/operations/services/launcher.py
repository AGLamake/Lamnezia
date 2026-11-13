"""Starting operations from the web, bots or beat, and retrying failed ones."""

from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import translation

from apps.accounts.models import User
from apps.operations.models import Operation
from apps.operations.services.registry import operations
from apps.operations.tasks import run_operation


class OperationNotRetryableError(Exception):
    pass


def launch(
    kind: str,
    params: dict[str, Any] | None = None,
    *,
    user: User | None = None,
    subject: str = "",
    retry_of: Operation | None = None,
) -> Operation:
    """Save a pending operation and queue it once the current transaction commits."""
    operations.get(kind)
    operation = Operation.objects.create(
        kind=kind,
        params=params or {},
        subject=subject,
        language=translation.get_language() or settings.LANGUAGE_CODE,
        created_by=user,
        retry_of=retry_of,
    )
    # The worker must see the saved row, so the job is queued only after commit.
    transaction.on_commit(lambda: run_operation.delay(operation.pk))
    return operation


def retry(operation: Operation, *, user: User | None = None) -> Operation:
    """Run a failed operation again with the same parameters.

    A repeated click returns the retry that already exists instead of starting another one.
    """
    with transaction.atomic():
        original = Operation.objects.select_for_update().get(pk=operation.pk)
        if not original.can_retry:
            raise OperationNotRetryableError(original.pk)
        existing = original.retries.first()
        if existing is not None:
            return existing
        return launch(
            original.kind, original.params, user=user, subject=original.subject, retry_of=original
        )
