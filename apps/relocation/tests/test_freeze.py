import pytest
from django.test import Client

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.operations.models import Operation, OperationStatus
from apps.relocation.models import MoveState
from apps.relocation.services.freeze import MOVING_ERROR, TaskGate, frozen_move
from apps.relocation.tasks import PanelTask
from apps.relocation.tests.helpers import make_move
from config.celery import app

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("state", "frozen"),
    [
        (MoveState.PREPARING, True),
        (MoveState.READY, True),
        (MoveState.LEFT, True),
        (MoveState.ARRIVED, False),
        (MoveState.CANCELLED, False),
        (MoveState.FAILED, False),
    ],
)
def test_only_an_unfinished_or_left_move_freezes_the_panel(state: MoveState, frozen: bool) -> None:
    move = make_move(state)

    assert (frozen_move() == move) is frozen


def test_the_latest_move_decides() -> None:
    make_move(MoveState.LEFT)
    make_move(MoveState.CANCELLED)

    assert frozen_move() is None


def test_without_a_move_every_task_runs() -> None:
    assert TaskGate().allows("bots.notify", ())


def test_a_frozen_panel_runs_only_the_moves_own_operations() -> None:
    make_move()
    own = Operation.objects.create(kind="relocation.confirm")
    other = Operation.objects.create(kind="keys.issue")

    gate = TaskGate()

    assert gate.allows("operations.run", (own.pk,))
    assert gate.allows("operations.fail_stale", ())
    assert not gate.allows("monitoring.poll", ())
    assert not gate.allows("operations.run", (other.pk,))
    other.refresh_from_db()
    assert (other.status, other.error_code) == (OperationStatus.FAILED, MOVING_ERROR)


def test_a_running_operation_is_not_refused() -> None:
    make_move()
    running = Operation.objects.create(kind="keys.issue", status=OperationStatus.RUNNING)

    assert not TaskGate().allows("operations.run", (running.pk,))
    running.refresh_from_db()
    assert running.status == OperationStatus.RUNNING


def test_every_task_of_the_panel_skips_jobs_of_a_frozen_panel() -> None:
    calls: list[int] = []

    @app.task(name="tests.relocation.job")
    def job(value: int) -> int:
        calls.append(value)
        return value

    assert isinstance(job, PanelTask)
    assert job(1) == 1
    make_move()
    assert job(2) is None
    assert job.delay(3).result is None
    assert calls == [1]


def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def test_a_frozen_panel_answers_changes_with_503_and_still_reads(client: Client) -> None:
    admin_client(client)
    make_move(MoveState.LEFT)

    page = client.get("/backups/panel/")
    change = client.post("/backups/panel/", {"password": "x"})

    assert page.status_code == 200
    assert "read-only" in page.content.decode()
    assert change.status_code == 503
    assert change["Retry-After"]
    assert "read-only" in change.content.decode()


def test_the_moves_own_buttons_and_signing_out_work_while_frozen(client: Client) -> None:
    admin_client(client)
    move = make_move(MoveState.READY)

    assert client.post(f"/backups/move/{move.pk}/cancel/").status_code == 302
    assert client.post("/accounts/logout/").status_code != 503


def test_without_a_move_changes_pass(client: Client) -> None:
    admin_client(client)
    make_move(MoveState.CANCELLED)

    assert client.post("/backups/panel/", {"password": "x"}).status_code == 200


def test_shared_tasks_of_the_apps_use_the_panel_base() -> None:
    from apps.operations.tasks import run_operation  # noqa: PLC0415

    assert isinstance(app.tasks[run_operation.name], PanelTask)
