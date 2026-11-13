"""A `fallback` event goes on to the user's other channels until one of them takes it."""

import json

import pytest
from django.conf import settings
from django.core import mail
from django.core.mail.backends.locmem import EmailBackend

from apps.accounts.models import LinkedAccount, Messenger, NotifyVia, User
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.registration import configure as configure_site
from apps.notifications.events import Event
from apps.notifications.models import Delivery, DeliveryStatus
from apps.notifications.services.channels import DeliveryError
from apps.notifications.services.delivery import emit
from apps.notifications.tests.conftest import FakeHttp

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("commit")]

TELEGRAM_SEND = "https://api.telegram.org/bot999:panel/sendMessage"
DISCORD_OPEN = "https://discord.com/api/v10/users/@me/channels"
REMINDER = {"user": "alice", "topic": "No handshake", "url": "https://vpn.example/support/1/"}


def alice(notify_via: str) -> User:
    """Alice with an email address and both messengers linked."""
    configure_site(telegram_bot_token="999:panel", discord_bot_token="panel-token")
    user: User = UserFactory(email="alice@example.com", notify_via=notify_via)
    LinkedAccount.objects.create(user=user, messenger=Messenger.TELEGRAM, external_id="1001")
    LinkedAccount.objects.create(user=user, messenger=Messenger.DISCORD, external_id="501")
    return user


def remind(user: User) -> Delivery:
    emit(Event.REQUEST_REMINDER, REMINDER, user=user)
    return Delivery.objects.get()


def test_the_chosen_channel_that_takes_it_is_the_only_one(http: FakeHttp) -> None:
    user = alice(NotifyVia.TELEGRAM)

    remind(user)

    [post] = http.posts
    assert post.url == TELEGRAM_SEND
    assert REMINDER["url"] in json.loads(post.body)["text"]
    assert mail.outbox == []


def test_a_blocked_bot_hands_it_to_the_next_channel(http: FakeHttp) -> None:
    user = alice(NotifyVia.TELEGRAM)
    http.failing[TELEGRAM_SEND] = DeliveryError("HTTP 403: bot was blocked", retryable=False)
    http.answers[DISCORD_OPEN] = b'{"id": "77"}'

    delivery = remind(user)

    opened, posted = http.posts
    assert json.loads(opened.body) == {"recipient_id": "501"}
    assert "No handshake" in json.loads(posted.body)["content"]
    assert mail.outbox == []
    delivery.refresh_from_db()
    assert delivery.status == DeliveryStatus.SENT


def test_messengers_that_do_not_work_lead_to_email(http: FakeHttp) -> None:
    user = alice(NotifyVia.DISCORD)
    http.failing[DISCORD_OPEN] = DeliveryError("Discord refused", retryable=True)
    http.failing[TELEGRAM_SEND] = DeliveryError("HTTP 403", retryable=False)

    remind(user)

    [letter] = mail.outbox
    assert letter.to == ["alice@example.com"]


def test_an_unlinked_choice_goes_to_the_next_channel(http: FakeHttp) -> None:
    configure_site(telegram_bot_token="999:panel")
    user = UserFactory(email="alice@example.com", notify_via=NotifyVia.DISCORD)
    LinkedAccount.objects.create(user=user, messenger=Messenger.TELEGRAM, external_id="1001")

    remind(user)

    assert [post.url for post in http.posts] == [TELEGRAM_SEND]
    assert mail.outbox == []


@pytest.fixture
def mail_server_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: EmailBackend, messages: object) -> int:
        raise OSError("connection refused")

    monkeypatch.setattr(EmailBackend, "send_messages", refuse)


def test_a_mail_that_did_not_go_hands_it_to_the_messengers(
    http: FakeHttp, mail_server_down: None
) -> None:
    remind(alice(NotifyVia.EMAIL))

    assert [post.url for post in http.posts] == [TELEGRAM_SEND]


def test_when_nothing_takes_it_the_errors_are_kept_and_retried(
    http: FakeHttp, mail_server_down: None
) -> None:
    user = alice(NotifyVia.TELEGRAM)
    http.failing[TELEGRAM_SEND] = DeliveryError("HTTP 403", retryable=False)
    http.failing[DISCORD_OPEN] = DeliveryError("connection failed", retryable=True)

    delivery = remind(user)

    delivery.refresh_from_db()
    assert delivery.attempts == settings.NOTIFICATION_MAX_ATTEMPTS  # Discord might answer later
    assert (delivery.status, delivery.error) == (
        DeliveryStatus.FAILED,
        "HTTP 403; connection failed; mail server: connection refused",
    )


def test_other_user_events_still_fall_back_to_email_only(http: FakeHttp) -> None:
    user = alice(NotifyVia.TELEGRAM)
    http.failing[TELEGRAM_SEND] = DeliveryError("HTTP 403", retryable=False)

    emit(Event.ACCESS_EXPIRING, {"user": user.username, "until": "2026-11-06"}, user=user)

    assert http.posts == []
    assert mail.outbox == []
    assert Delivery.objects.get().status == DeliveryStatus.FAILED


def test_the_notification_keeps_the_channel_that_took_it(http: FakeHttp) -> None:
    user = alice(NotifyVia.TELEGRAM)
    http.failing[TELEGRAM_SEND] = DeliveryError("HTTP 403: bot was blocked", retryable=False)
    http.failing[DISCORD_OPEN] = DeliveryError("Discord refused", retryable=False)

    delivery = remind(user)

    delivery.notification.refresh_from_db()
    assert delivery.notification.sent_via == NotifyVia.EMAIL


def test_a_message_no_channel_took_reached_nobody(http: FakeHttp, mail_server_down: None) -> None:
    user = alice(NotifyVia.TELEGRAM)
    http.failing[TELEGRAM_SEND] = DeliveryError("HTTP 403", retryable=False)
    http.failing[DISCORD_OPEN] = DeliveryError("Discord refused", retryable=False)

    delivery = remind(user)

    delivery.notification.refresh_from_db()
    assert delivery.notification.sent_via == ""


def test_the_skipped_channel_is_never_tried(http: FakeHttp) -> None:
    user = alice(NotifyVia.DISCORD)
    http.failing[DISCORD_OPEN] = DeliveryError("Discord refused", retryable=False)

    emit(Event.SUPPORT_ANSWER, REMINDER, user=user, skip_via=NotifyVia.TELEGRAM)

    assert http.to(TELEGRAM_SEND) == []
    [letter] = mail.outbox
    assert letter.to == ["alice@example.com"]


def test_with_only_the_skipped_channel_left_nothing_is_sent(http: FakeHttp) -> None:
    user = UserFactory(email="alice@example.com", notify_via=NotifyVia.DISCORD)

    emit(Event.SUPPORT_ANSWER, REMINDER, user=user, skip_via=NotifyVia.EMAIL)

    assert mail.outbox == []
    delivery = Delivery.objects.get()
    assert (delivery.status, delivery.error) == (
        DeliveryStatus.FAILED,
        "the user's other channels already have it",
    )
