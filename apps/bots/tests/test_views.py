from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import Messenger, Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.bots.models import BotRuntime, BotStatus

pytestmark = pytest.mark.django_db

STATUS = "/settings/bots/"


@pytest.fixture
def admin(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


@pytest.fixture
def discord() -> BotRuntime:
    return BotRuntime.objects.create(
        messenger=Messenger.DISCORD,
        status=BotStatus.RUNNING,
        identity="Lamnezia#0001",
        servers=[{"id": "42", "name": "Friends", "members": 7}],
        started_at=timezone.now(),
        seen_at=timezone.now(),
    )


@pytest.mark.parametrize("role", [Role.MODERATOR, Role.USER])
def test_only_an_administrator_sees_and_controls_the_bots(
    client: Client, discord: BotRuntime, role: str
) -> None:
    client.force_login(UserFactory(role=role))

    assert client.get(STATUS).status_code == 403
    assert client.post(f"{STATUS}discord/turn/", {"on": "0"}).status_code == 403
    discord.refresh_from_db()
    assert discord.enabled


def test_the_block_shows_the_bot_its_servers_and_a_missing_report(
    admin: Client, discord: BotRuntime
) -> None:
    html = admin.get(STATUS).content.decode()

    assert "Lamnezia#0001" in html
    assert "Friends" in html
    assert 'hx-trigger="every 5s"' in html
    assert "No report from the bots service about this bot yet." in html  # Telegram has no row


def test_a_bot_whose_service_went_quiet_is_not_responding(
    admin: Client, discord: BotRuntime
) -> None:
    discord.seen_at = timezone.now() - timedelta(minutes=5)
    discord.save()

    assert "not responding" in admin.get(STATUS).content.decode()


def test_turning_off_and_on_is_kept_and_audited(admin: Client, discord: BotRuntime) -> None:
    admin.post(f"{STATUS}discord/turn/", {"on": "0"})
    discord.refresh_from_db()
    assert not discord.enabled

    admin.post(f"{STATUS}discord/turn/", {"on": "1"})
    discord.refresh_from_db()
    assert discord.enabled

    actions = list(AuditEvent.objects.order_by("pk").values_list("action", flat=True))
    assert actions == ["bots.turn_off", "bots.turn_on"]


def test_a_restart_is_asked_of_the_service(admin: Client, discord: BotRuntime) -> None:
    response = admin.post(f"{STATUS}discord/restart/")

    assert response.status_code == 302
    discord.refresh_from_db()
    assert discord.restart_requested_at is not None
    assert AuditEvent.objects.get().action == "bots.restart"


def test_leaving_a_server_is_asked_once_and_audited(admin: Client, discord: BotRuntime) -> None:
    admin.post(f"{STATUS}discord/leave/42/")
    admin.post(f"{STATUS}discord/leave/42/")

    discord.refresh_from_db()
    assert discord.leave_requests == ["42"]
    event = AuditEvent.objects.filter(action="bots.leave").first()
    assert event is not None
    assert event.data == {"server": "42", "name": "Friends"}


def test_an_unknown_server_or_bot_is_not_found(admin: Client, discord: BotRuntime) -> None:
    assert admin.post(f"{STATUS}discord/leave/7/").status_code == 404
    assert admin.post(f"{STATUS}telegram/restart/").status_code == 404
