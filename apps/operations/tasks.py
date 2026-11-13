from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.core.models import Cleanup
from apps.operations.services.retention import prune_operations
from apps.operations.services.runner import OperationRunner
from apps.operations.services.stale import fail_stale_operations


@shared_task(name="operations.run")
def run_operation(operation_id: int) -> None:
    OperationRunner().run(operation_id)


@shared_task(name="operations.fail_stale")
def fail_stale() -> int:
    return fail_stale_operations(timedelta(seconds=settings.OPERATION_TIMEOUT))


@shared_task(name="operations.prune")
def prune() -> int:
    before = timezone.now() - timedelta(days=settings.OPERATION_RETENTION_DAYS)
    return Cleanup.record("operations.prune", prune_operations(before))
