from pathlib import Path

import pytest
from django.test import Client

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.relocation.errors import MoveError, MoveErrorCode
from apps.relocation.models import DomainStatus, MoveState, PanelMove
from apps.relocation.services import domain
from apps.relocation.services.domain import DomainCheck
from apps.relocation.services.shutdown import STOP_KIND, STOP_REQUEST, OldCopyShutdown
from apps.relocation.tests.helpers import FakeMachine, fake_connect, make_move

pytestmark = pytest.mark.django_db

SHUTDOWN = "apps.relocation.services.shutdown"
DOMAIN = "panel.example.com"
NEW_MACHINE = "198.51.100.7"
OLD_MACHINE = "203.0.113.10"


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> dict[str, set[str]]:
    table: dict[str, set[str]] = {DOMAIN: {NEW_MACHINE}}
    monkeypatch.setattr(domain, "a_records", lambda name: table.get(name, set()))
    return table


@pytest.fixture
def archives(settings: object, tmp_path: Path) -> Path:
    settings.PANEL_ARCHIVE_DIR = tmp_path  # type: ignore[attr-defined]
    return tmp_path


def moved(state: MoveState = MoveState.LEFT, *, auto_stop: bool = True) -> PanelMove:
    move = make_move(MoveState.READY, OperationStatus.SUCCEEDED, auto_stop=auto_stop)
    move.target_host = NEW_MACHINE
    move.save()
    move.mark(state)
    return move


def stops() -> list[Operation]:
    return list(Operation.objects.filter(kind=STOP_KIND))


def shut_down(monkeypatch: pytest.MonkeyPatch, move: PanelMove, fake: FakeMachine) -> None:
    fake_connect(monkeypatch, SHUTDOWN)
    monkeypatch.setattr(f"{SHUTDOWN}.TargetMachine", fake)
    try:
        OldCopyShutdown(move, OperationContext(OperationFactory())).run()
    finally:
        move.refresh_from_db()


def test_a_confirmed_move_with_the_domain_switched_asks_to_stop(
    records: dict[str, set[str]],
) -> None:
    move = moved()

    DomainCheck(DOMAIN).run()
    DomainCheck(DOMAIN).run()

    [stop] = stops()
    assert stop.params == {"move_id": move.pk}
    move.refresh_from_db()
    assert move.operation == stop


def test_nothing_stops_with_the_option_off(records: dict[str, set[str]]) -> None:
    move = moved(auto_stop=False)

    assert DomainCheck(DOMAIN).run() == move
    assert stops() == []


def test_nothing_stops_while_the_domain_points_elsewhere(records: dict[str, set[str]]) -> None:
    moved()
    records[DOMAIN] = {NEW_MACHINE, OLD_MACHINE}

    DomainCheck(DOMAIN).run()

    assert stops() == []


@pytest.mark.parametrize("state", [MoveState.PREPARING, MoveState.READY, MoveState.ARRIVED])
def test_nothing_stops_without_a_confirmed_move(
    state: MoveState, records: dict[str, set[str]]
) -> None:
    make_move(state, domain_status=DomainStatus.POINTED)

    DomainCheck(DOMAIN).run()

    assert stops() == []


def test_the_option_turned_on_later_still_stops(records: dict[str, set[str]]) -> None:
    move = moved(auto_stop=False)
    DomainCheck(DOMAIN).run()
    PanelMove.objects.filter(pk=move.pk).update(auto_stop=True)

    DomainCheck(DOMAIN).run()

    assert len(stops()) == 1


def test_the_operation_leaves_the_request_for_the_host(
    monkeypatch: pytest.MonkeyPatch, archives: Path
) -> None:
    move = moved()
    PanelMove.objects.filter(pk=move.pk).update(domain_status=DomainStatus.POINTED)
    move.refresh_from_db()
    fake = FakeMachine()

    shut_down(monkeypatch, move, fake)

    assert fake.calls == ["verify_live"]
    assert (archives / STOP_REQUEST).read_text() == f"{move.pk}\n"
    assert move.stop_requested_at is not None
    assert not move.should_stop


def test_a_silent_new_copy_keeps_the_old_one_running(
    monkeypatch: pytest.MonkeyPatch, archives: Path
) -> None:
    move = moved()
    PanelMove.objects.filter(pk=move.pk).update(domain_status=DomainStatus.POINTED)
    move.refresh_from_db()

    with pytest.raises(MoveError):
        shut_down(monkeypatch, move, FakeMachine(fail_at="verify_live"))

    assert not (archives / STOP_REQUEST).exists()
    assert move.stop_requested_at is None


def test_the_operation_refuses_once_the_option_is_off(
    monkeypatch: pytest.MonkeyPatch, archives: Path
) -> None:
    move = moved(auto_stop=False)
    PanelMove.objects.filter(pk=move.pk).update(domain_status=DomainStatus.POINTED)
    move.refresh_from_db()

    with pytest.raises(MoveError) as error:
        shut_down(monkeypatch, move, FakeMachine())

    assert error.value.code == MoveErrorCode.WRONG_STATE
    assert not (archives / STOP_REQUEST).exists()


@pytest.fixture
def admin(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def test_the_option_is_switched_on_the_frozen_panel(admin: Client) -> None:
    move = moved()
    assert move.frozen

    response = admin.post(f"/backups/move/{move.pk}/auto-stop/", {"enabled": "0"})

    assert response["Location"] == f"/backups/move/{move.pk}/"
    move.refresh_from_db()
    assert not move.auto_stop
    assert AuditEvent.objects.filter(action="relocation.auto_stop").exists()
    assert "Turn on" in admin.get(f"/backups/move/{move.pk}/").content.decode()


def test_the_option_is_fixed_once_the_stop_is_requested(admin: Client) -> None:
    move = moved()
    PanelMove.objects.filter(pk=move.pk).update(stop_requested_at=move.handed_over_at)

    admin.post(f"/backups/move/{move.pk}/auto-stop/", {"enabled": "0"})

    move.refresh_from_db()
    assert move.auto_stop
    page = admin.get(f"/backups/move/{move.pk}/").content.decode()
    assert "asked its host to stop" in page


def test_the_move_form_passes_the_option(admin: Client) -> None:
    target = make_move(MoveState.CANCELLED).target
    assert target is not None

    response = admin.post("/backups/move/", {"target": target.pk, "understood": "on"})

    assert response.status_code == 302
    assert PanelMove.latest() is not None
    assert not PanelMove.latest().auto_stop  # type: ignore[union-attr]
