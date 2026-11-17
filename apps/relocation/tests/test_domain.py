import pytest
from django.test import Client, override_settings

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.operations.models import OperationStatus, OperationStep, StepStatus
from apps.relocation.models import DomainStatus, MoveState, PanelMove
from apps.relocation.services import domain
from apps.relocation.services.domain import DomainCheck, panel_domain
from apps.relocation.services.freeze import TaskGate
from apps.relocation.tasks import CHECK_DOMAIN_TASK, check_domain
from apps.relocation.tests.helpers import make_move

pytestmark = pytest.mark.django_db

DOMAIN = "panel.example.com"
NEW_MACHINE = "198.51.100.7"
OLD_MACHINE = "203.0.113.10"


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> dict[str, set[str]]:
    """A records the check sees; tests change them as the admin would in the DNS."""
    table: dict[str, set[str]] = {DOMAIN: {OLD_MACHINE}}
    monkeypatch.setattr(domain, "a_records", lambda name: table.get(name, set()))
    return table


def handed_over(state: MoveState = MoveState.LEFT) -> PanelMove:
    move = make_move(MoveState.READY, OperationStatus.SUCCEEDED)
    move.target_host = NEW_MACHINE
    move.save()
    move.mark(state)
    return move


def check() -> PanelMove:
    move = DomainCheck(DOMAIN).run()
    assert move is not None
    return move


def test_the_hand_over_starts_the_timer_once() -> None:
    move = handed_over()
    started = move.handed_over_at
    assert started is not None

    move.mark(MoveState.LEFT)

    assert move.handed_over_at == started


def test_the_status_follows_the_a_record(records: dict[str, set[str]]) -> None:
    move = handed_over()
    assert check().domain_status == DomainStatus.ELSEWHERE
    assert move.awaits_domain

    records[DOMAIN] = {NEW_MACHINE}
    move = check()

    assert move.domain_status == DomainStatus.POINTED
    assert move.domain_addresses == NEW_MACHINE
    assert move.domain_checked_at is not None
    assert not move.awaits_domain


def test_an_a_record_left_on_the_old_machine_is_not_a_switch(
    records: dict[str, set[str]],
) -> None:
    handed_over()
    records[DOMAIN] = {NEW_MACHINE, OLD_MACHINE}

    assert check().domain_status == DomainStatus.ELSEWHERE


def test_a_domain_without_a_records(records: dict[str, set[str]]) -> None:
    handed_over()
    records.clear()

    assert check().domain_status == DomainStatus.UNRESOLVED


def test_a_host_name_of_the_new_machine_is_resolved(records: dict[str, set[str]]) -> None:
    move = handed_over()
    move.target_host = "new.example.net"
    move.save()
    records.update({"new.example.net": {NEW_MACHINE}, DOMAIN: {NEW_MACHINE}})

    assert check().domain_status == DomainStatus.POINTED


@pytest.mark.parametrize("state", [MoveState.PREPARING, MoveState.READY, MoveState.CANCELLED])
def test_the_domain_is_not_checked_before_the_hand_over(
    state: MoveState, records: dict[str, set[str]]
) -> None:
    move = make_move(state)

    assert DomainCheck(DOMAIN).run() is None
    move.refresh_from_db()
    assert move.domain_status == DomainStatus.UNCHECKED


def test_checks_stop_once_the_domain_points_to_the_new_machine(
    records: dict[str, set[str]],
) -> None:
    handed_over(MoveState.ARRIVED)
    records[DOMAIN] = {NEW_MACHINE}
    check()
    records[DOMAIN] = {OLD_MACHINE}

    assert DomainCheck(DOMAIN).run() is None


@override_settings(SITE_URL="http://203.0.113.10:8000")
def test_a_panel_opened_by_an_ip_address_has_no_domain_to_check(
    records: dict[str, set[str]],
) -> None:
    handed_over()

    assert panel_domain() == ""
    assert DomainCheck.current().run() is None


@override_settings(SITE_URL=f"https://{DOMAIN}")
def test_the_task_runs_on_the_frozen_old_panel(records: dict[str, set[str]]) -> None:
    move = handed_over()
    assert move.frozen
    records[DOMAIN] = {NEW_MACHINE}

    assert TaskGate().allows(CHECK_DOMAIN_TASK, ())
    assert check_domain() == DomainStatus.POINTED


@pytest.fixture
def admin(client: Client, monkeypatch: pytest.MonkeyPatch) -> Client:
    def no_lookups(name: str) -> set[str]:
        raise AssertionError("a page must not look up DNS")

    monkeypatch.setattr(domain, "a_records", no_lookups)
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def test_the_screen_shows_the_steps_while_the_operation_runs(admin: Client) -> None:
    move = make_move(MoveState.PREPARING, OperationStatus.RUNNING)
    assert move.operation is not None
    move.operation.params = {"move_id": move.pk}
    move.operation.save()
    OperationStep.objects.create(
        operation=move.operation,
        position=1,
        title="Check the new machine",
        status=StepStatus.RUNNING,
    )

    page = admin.get(f"/backups/move/{move.pk}/").content.decode()

    assert "Check the new machine" in page
    assert f"/backups/move/{move.pk}/progress/" in page
    assert "Panel domain" not in page


@override_settings(SITE_URL=f"https://{DOMAIN}")
def test_the_screen_shows_the_status_of_the_a_record(admin: Client) -> None:
    move = handed_over()
    url = f"/backups/move/{move.pk}/progress/"
    waiting = admin.get(url).content.decode()
    assert "not checked yet" in waiting
    assert f"Point the A record of the domain to {NEW_MACHINE}" in waiting
    assert "hx-trigger" in waiting

    PanelMove.objects.filter(pk=move.pk).update(
        domain_status=DomainStatus.POINTED, domain_addresses=NEW_MACHINE
    )
    done = admin.get(url).content.decode()

    assert "points to the new machine" in done
    assert "docker compose down" in done
    assert "hx-trigger" not in done


def test_actions_lead_to_the_screen(admin: Client) -> None:
    move = make_move(MoveState.READY, OperationStatus.SUCCEEDED)

    response = admin.post(f"/backups/move/{move.pk}/cancel/")

    assert response["Location"] == f"/backups/move/{move.pk}/"
