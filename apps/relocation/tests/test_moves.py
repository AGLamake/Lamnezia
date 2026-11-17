import io

import pytest
from django.core.management import CommandError, call_command
from django.test import Client

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.operations.models import Operation, OperationStatus
from apps.relocation.errors import MoveError, MoveErrorCode
from apps.relocation.models import MoveState, PanelMove
from apps.relocation.services.completion import arrive_here
from apps.relocation.services.moves import (
    CANCEL_KIND,
    CONFIRM_KIND,
    PREPARE_KIND,
    cancel_move,
    confirm_move,
    start_move,
)
from apps.relocation.tests.helpers import make_move
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def test_starting_freezes_the_panel_and_queues_the_preparation() -> None:
    admin = UserFactory(role=Role.ADMIN)
    server = ServerFactory()

    move = start_move(server, admin, "10.0.0.1")

    assert (move.state, move.target_host, move.created_by) == (
        MoveState.PREPARING,
        server.host,
        admin,
    )
    assert move.operation is not None
    assert (move.operation.kind, move.operation.params) == (PREPARE_KIND, {"move_id": move.pk})
    assert AuditEvent.objects.filter(action=PREPARE_KIND).count() == 1


def test_a_second_move_is_refused_while_the_first_holds_the_panel() -> None:
    admin = UserFactory(role=Role.ADMIN)
    start_move(ServerFactory(), admin, None)

    with pytest.raises(MoveError) as error:
        start_move(ServerFactory(), admin, None)
    assert error.value.code == MoveErrorCode.WRONG_STATE


def test_a_move_waits_for_running_operations() -> None:
    Operation.objects.create(kind="keys.issue", status=OperationStatus.RUNNING)

    with pytest.raises(MoveError) as error:
        start_move(ServerFactory(), UserFactory(role=Role.ADMIN), None)
    assert error.value.code == MoveErrorCode.BUSY
    assert not PanelMove.objects.exists()


def test_confirming_a_ready_move_queues_the_hand_over() -> None:
    move = make_move(MoveState.READY, OperationStatus.SUCCEEDED)

    confirmed = confirm_move(move.pk, UserFactory(role=Role.ADMIN), None)

    assert confirmed.confirm_started
    assert confirmed.operation is not None
    assert confirmed.operation.kind == CONFIRM_KIND
    assert AuditEvent.objects.filter(action=CONFIRM_KIND).exists()


@pytest.mark.parametrize(
    ("state", "status"),
    [
        (MoveState.PREPARING, OperationStatus.RUNNING),
        (MoveState.READY, OperationStatus.PENDING),
        (MoveState.LEFT, OperationStatus.SUCCEEDED),
    ],
)
def test_confirming_needs_a_ready_and_idle_move(state: MoveState, status: OperationStatus) -> None:
    move = make_move(state, status)

    with pytest.raises(MoveError) as error:
        confirm_move(move.pk, UserFactory(role=Role.ADMIN), None)
    assert error.value.code == MoveErrorCode.WRONG_STATE


def test_a_failed_move_can_be_cancelled_but_a_left_one_cannot() -> None:
    admin = UserFactory(role=Role.ADMIN)
    failed = make_move(MoveState.FAILED, OperationStatus.FAILED)

    assert cancel_move(failed.pk, admin, None).operation.kind == CANCEL_KIND  # type: ignore[union-attr]

    left = make_move(MoveState.LEFT, OperationStatus.SUCCEEDED)
    with pytest.raises(MoveError):
        cancel_move(left.pk, admin, None)


def test_the_copy_arrives_once_and_ends_the_archived_operation() -> None:
    move = make_move(MoveState.PREPARING, OperationStatus.RUNNING)

    assert arrive_here(move.pk).state == MoveState.ARRIVED
    assert arrive_here(move.pk).state == MoveState.ARRIVED
    move.refresh_from_db()
    assert move.operation is not None
    assert move.operation.status == OperationStatus.SUCCEEDED


def test_a_cancelled_move_never_arrives() -> None:
    move = make_move(MoveState.CANCELLED)

    with pytest.raises(MoveError):
        arrive_here(move.pk)


def test_the_command_reports_the_state_and_arrives() -> None:
    move = make_move()
    out = io.StringIO()

    call_command("relocation", "status", str(move.pk), stdout=out)
    call_command("relocation", "status", "999", stdout=out)
    call_command("relocation", "arrive", str(move.pk), stdout=out)

    assert out.getvalue().split() == ["preparing", "missing", "arrived"]
    with pytest.raises(CommandError):
        call_command("relocation", "arrive", "999")


def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def test_the_page_starts_a_move_only_when_understood(client: Client) -> None:
    admin_client(client)
    server = ServerFactory()
    assert client.get("/backups/move/").status_code == 200

    refused = client.post("/backups/move/", {"target": server.pk})
    assert refused.status_code == 200
    assert not PanelMove.objects.exists()

    started = client.post("/backups/move/", {"target": server.pk, "understood": "on"})
    move = PanelMove.objects.get()
    assert started["Location"] == f"/operations/{move.operation_id}/"
    page = client.get("/backups/move/").content.decode()
    assert "Cancel the move" not in page
    assert server.host in page


def test_the_page_shows_the_buttons_and_the_dns_hint(client: Client) -> None:
    admin_client(client)
    ready = make_move(MoveState.READY, OperationStatus.SUCCEEDED)
    page = client.get("/backups/move/").content.decode()
    assert "Confirm the move" in page
    assert "Cancel the move" in page

    ready.mark(MoveState.LEFT)
    page = client.get("/backups/move/").content.decode()
    assert f"Point the panel domain to {ready.target_host}" in page
    assert "Confirm the move" not in page


def test_a_refused_action_returns_to_the_page_with_the_reason(client: Client) -> None:
    admin_client(client)
    move = make_move(MoveState.LEFT, OperationStatus.SUCCEEDED)

    response = client.post(f"/backups/move/{move.pk}/confirm/", follow=True)

    assert response.redirect_chain[-1][0] == "/backups/move/"
    assert str(MoveError(MoveErrorCode.WRONG_STATE).message) in response.content.decode()


def test_only_admins_open_the_page(client: Client) -> None:
    client.force_login(UserFactory(role=Role.USER))

    assert client.get("/backups/move/").status_code in (302, 403)
