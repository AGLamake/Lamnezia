import pytest

from apps.operations.models import OperationStatus, StepStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.registry import OperationRegistry
from apps.operations.services.runner import UNEXPECTED_ERROR, OperationRunner
from apps.operations.tests.factories import OperationFactory

pytestmark = pytest.mark.django_db


def runner_with(kind: str, calls: list[str], error: Exception | None = None) -> OperationRunner:
    registry = OperationRegistry()

    @registry.register(kind, "Test")
    def handler(ctx: OperationContext) -> None:
        calls.append(kind)
        with ctx.step("Only step"):
            if error:
                raise error

    return OperationRunner(registry)


def test_success_records_times_and_steps() -> None:
    operation = OperationFactory()
    calls: list[str] = []
    runner_with("test.noop", calls).run(operation.pk)

    operation.refresh_from_db()
    assert calls == ["test.noop"]
    assert operation.status == OperationStatus.SUCCEEDED
    assert operation.started_at and operation.finished_at
    assert operation.steps.get().status == StepStatus.SUCCEEDED


@pytest.mark.parametrize("status", [OperationStatus.RUNNING, OperationStatus.SUCCEEDED])
def test_operation_that_is_not_pending_is_not_run_again(status: OperationStatus) -> None:
    operation = OperationFactory(status=status)
    calls: list[str] = []
    runner_with("test.noop", calls).run(operation.pk)

    operation.refresh_from_db()
    assert calls == []
    assert operation.status == status


def test_unexpected_error_gets_a_generic_message() -> None:
    operation = OperationFactory()
    runner_with("test.noop", [], KeyError("secret detail")).run(operation.pk)

    operation.refresh_from_db()
    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == UNEXPECTED_ERROR
    assert operation.error_message == "Unexpected error."
    assert "secret" not in operation.error_message + operation.error_hint
    assert operation.steps.get().status == StepStatus.FAILED


def test_unknown_kind_fails_the_operation() -> None:
    operation = OperationFactory(kind="test.removed")
    calls: list[str] = []
    runner_with("test.noop", calls).run(operation.pk)

    operation.refresh_from_db()
    assert calls == []
    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == UNEXPECTED_ERROR


def test_log_outside_a_step_is_an_error() -> None:
    ctx = OperationContext(OperationFactory())

    with pytest.raises(RuntimeError):
        ctx.log("hello")
