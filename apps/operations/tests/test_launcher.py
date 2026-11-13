from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.operations.models import Operation, OperationStatus, StepStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import OperationNotRetryableError, launch, retry
from apps.operations.services.registry import UnknownOperationKindError
from apps.operations.tests.conftest import FakeServer, Register
from apps.operations.tests.factories import OperationFactory

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]


def add_peer_kind(server: FakeServer) -> Callable[[OperationContext], None]:
    def add_peer(ctx: OperationContext) -> None:
        with ctx.step("Add peer"):
            server.add_peer(ctx.params["peer"])
            ctx.log(f"peer {ctx.params['peer']} added")
        with ctx.step("Apply config"):
            server.sync()

    return add_peer


def test_launch_queues_the_job_after_commit(
    register_kind: Register, django_capture_on_commit_callbacks: OnCommit
) -> None:
    server = FakeServer()
    register_kind("test.add_peer", add_peer_kind(server))
    user = UserFactory()

    with django_capture_on_commit_callbacks(execute=True):
        operation = launch("test.add_peer", {"peer": "alice"}, user=user, subject="Amsterdam")
        assert server.peers == []

    operation.refresh_from_db()
    assert operation.status == OperationStatus.SUCCEEDED
    assert (operation.created_by, operation.subject) == (user, "Amsterdam")
    assert server.peers == ["alice"]


def test_launch_rejects_unknown_kind() -> None:
    with pytest.raises(UnknownOperationKindError):
        launch("test.typo")
    assert not Operation.objects.exists()


def test_failed_step_keeps_its_log_and_error(
    register_kind: Register, django_capture_on_commit_callbacks: OnCommit
) -> None:
    register_kind("test.add_peer", add_peer_kind(FakeServer(sync_failures=1)))

    with django_capture_on_commit_callbacks(execute=True):
        operation = launch("test.add_peer", {"peer": "alice"})

    operation.refresh_from_db()
    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == "command_failed"
    assert operation.error_message == "A command on the server failed."
    assert operation.error_hint
    steps = list(operation.steps.values_list("title", "status", "log"))
    assert steps == [
        ("Add peer", StepStatus.SUCCEEDED, "peer alice added\n"),
        ("Apply config", StepStatus.FAILED, ""),
    ]


def test_retry_of_idempotent_operation_applies_the_change_once(
    register_kind: Register, django_capture_on_commit_callbacks: OnCommit
) -> None:
    server = FakeServer(sync_failures=1)
    register_kind("test.add_peer", add_peer_kind(server))
    with django_capture_on_commit_callbacks(execute=True):
        failed = launch("test.add_peer", {"peer": "alice"}, subject="Amsterdam")

    with django_capture_on_commit_callbacks(execute=True):
        retried = retry(failed)

    retried.refresh_from_db()
    assert retried.status == OperationStatus.SUCCEEDED
    assert (retried.retry_of, retried.kind, retried.params, retried.subject) == (
        failed,
        failed.kind,
        failed.params,
        failed.subject,
    )
    assert server.peers == ["alice"]
    assert server.syncs == 1
    failed.refresh_from_db()
    assert failed.status == OperationStatus.FAILED


def test_second_retry_click_returns_the_first_retry(
    register_kind: Register, django_capture_on_commit_callbacks: OnCommit
) -> None:
    server = FakeServer(sync_failures=1)
    register_kind("test.add_peer", add_peer_kind(server))
    with django_capture_on_commit_callbacks(execute=True):
        failed = launch("test.add_peer", {"peer": "alice"})
    with django_capture_on_commit_callbacks(execute=True):
        first = retry(failed)
        second = retry(failed)

    assert first == second
    assert Operation.objects.count() == 2
    assert server.syncs == 1


@pytest.mark.parametrize("status", [OperationStatus.SUCCEEDED, OperationStatus.RUNNING])
def test_only_failed_operation_can_be_retried(status: OperationStatus) -> None:
    with pytest.raises(OperationNotRetryableError):
        retry(OperationFactory(status=status))
