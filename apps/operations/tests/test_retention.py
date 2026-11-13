from datetime import timedelta

import pytest
from django.utils import timezone

from apps.operations.models import Operation, OperationStatus, OperationStep
from apps.operations.services.retention import prune_operations
from apps.operations.tests.factories import OperationFactory, OperationStepFactory

pytestmark = pytest.mark.django_db


def finished(status: OperationStatus, days_ago: int) -> Operation:
    moment = timezone.now() - timedelta(days=days_ago)
    operation = OperationFactory(status=status)
    Operation.objects.filter(pk=operation.pk).update(created_at=moment, finished_at=moment)
    return operation


def test_old_finished_operations_are_deleted_with_their_steps() -> None:
    done = finished(OperationStatus.SUCCEEDED, 100)
    OperationStepFactory(operation=done)
    finished(OperationStatus.FAILED, 100)

    assert prune_operations(timezone.now() - timedelta(days=90)) == 2

    assert not Operation.objects.exists()
    assert not OperationStep.objects.exists()


def test_recent_and_unfinished_operations_are_kept() -> None:
    recent = finished(OperationStatus.SUCCEEDED, 10)
    running = OperationFactory(status=OperationStatus.RUNNING)
    Operation.objects.filter(pk=running.pk).update(created_at=timezone.now() - timedelta(days=100))

    assert prune_operations(timezone.now() - timedelta(days=90)) == 0

    assert set(Operation.objects.values_list("pk", flat=True)) == {recent.pk, running.pk}
