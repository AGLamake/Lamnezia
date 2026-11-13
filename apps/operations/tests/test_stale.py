from datetime import timedelta

import pytest
from django.utils import timezone

from apps.operations.models import Operation, OperationStatus, StepStatus
from apps.operations.services.stale import INTERRUPTED_ERROR, fail_stale_operations
from apps.operations.tests.factories import OperationFactory, OperationStepFactory

pytestmark = pytest.mark.django_db

TIMEOUT = timedelta(hours=1)


def aged(operation: Operation, age: timedelta) -> Operation:
    moment = timezone.now() - age
    Operation.objects.filter(pk=operation.pk).update(created_at=moment, started_at=moment)
    return operation


def test_old_pending_and_running_operations_fail() -> None:
    pending = aged(OperationFactory(), TIMEOUT * 2)
    running = aged(OperationFactory(status=OperationStatus.RUNNING), TIMEOUT * 2)
    step = OperationStepFactory(operation=running, status=StepStatus.RUNNING)

    assert fail_stale_operations(TIMEOUT) == 2

    for operation in (pending, running):
        operation.refresh_from_db()
        assert operation.status == OperationStatus.FAILED
        assert operation.error_code == INTERRUPTED_ERROR
        assert operation.can_retry
    step.refresh_from_db()
    assert step.status == StepStatus.FAILED


def test_recent_and_finished_operations_are_kept() -> None:
    recent = aged(OperationFactory(status=OperationStatus.RUNNING), TIMEOUT / 2)
    done = aged(OperationFactory(status=OperationStatus.SUCCEEDED), TIMEOUT * 2)

    assert fail_stale_operations(TIMEOUT) == 0

    recent.refresh_from_db()
    done.refresh_from_db()
    assert (recent.status, done.status) == (OperationStatus.RUNNING, OperationStatus.SUCCEEDED)
