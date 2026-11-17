import pytest
from django.core.management import CommandError, call_command
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.keys.models import AccessKey
from apps.notifications.models import Delivery, Notification
from apps.plans.models import Plan, UserLimits
from apps.plans.services.limits import EffectiveLimits
from apps.protocols.models import ProtocolInstance
from apps.servers.models import Server, ServerSample
from apps.support.models import Conversation, Kind, Message
from apps.traffic.models import TrafficCounter, TrafficHourly
from apps.traffic.services.limits import LimitEnforcer

MODELS = (
    User,
    Server,
    ServerSample,
    ProtocolInstance,
    Plan,
    AccessKey,
    TrafficCounter,
    TrafficHourly,
    Conversation,
    Message,
    Notification,
    Delivery,
)


def counts() -> dict[str, int]:
    return {model.__name__: model.objects.count() for model in MODELS}


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_seed_fills_every_table_once() -> None:
    call_command("seed_demo")
    first = counts()
    assert all(first.values()), first

    call_command("seed_demo")
    assert counts() == first


@pytest.mark.django_db
def test_seed_refuses_without_debug() -> None:
    with pytest.raises(CommandError):
        call_command("seed_demo")
    assert not Server.objects.exists()

    call_command("seed_demo", force=True)
    assert Server.objects.exists()


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_seeded_user_sees_keys() -> None:
    call_command("seed_demo")
    client = Client()
    client.force_login(User.objects.get(username="boris"))
    response = client.get("/portal/")
    assert response.status_code == 200
    assert "Android" in response.content.decode()


@pytest.mark.django_db
@override_settings(DEBUG=True, REQUIRE_ADMIN_2FA=False)
@pytest.mark.parametrize(
    ("url", "text"),
    [
        ("/servers/", "Frankfurt"),
        ("/keys/", "iPad"),
        ("/plans/", "Premium"),
        ("/support/inbox/", "Helsinki is slow"),
        ("/notifications/", "Demo webhook"),
    ],
)
def test_seeded_admin_pages_show_data(url: str, text: str) -> None:
    call_command("seed_demo")
    client = Client()
    client.force_login(User.objects.get(username="admin"))
    response = client.get(url, follow=True)
    assert response.status_code == 200
    assert text in response.content.decode()


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_seeded_plan_counts_traffic_for_the_period() -> None:
    call_command("seed_demo")
    boris = User.objects.get(username="boris")
    limits = EffectiveLimits.of(UserLimits.objects.get(user=boris))
    period, _ = LimitEnforcer(timezone.now()).usage(boris, limits)
    assert period > 0


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_seeded_chat_has_a_staff_reply() -> None:
    call_command("seed_demo")
    chat = Conversation.objects.get(user__username="boris", kind=Kind.CHAT)
    authors = list(chat.messages.order_by("pk").values_list("author__username", flat=True))
    assert authors == ["boris", "moderator"]


@pytest.mark.django_db
@override_settings(DEBUG=True, LANGUAGE_CODE="ru")
def test_seeded_tickets_use_the_panel_language() -> None:
    call_command("seed_demo")
    assert Conversation.objects.filter(subject="Хельсинки тормозит").exists()
    assert Message.objects.filter(text="Здравствуйте! Можно оплатить сразу год?").exists()
