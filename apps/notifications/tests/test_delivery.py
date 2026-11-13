import json

import pytest
from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.utils import translation

from apps.accounts.models import LinkedAccount, Messenger, NotifyVia
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.registration import configure as configure_site
from apps.bots.services.session import Panels
from apps.notifications.events import Event
from apps.notifications.models import ChannelKind, Delivery, DeliveryStatus, Notification
from apps.notifications.services import personal
from apps.notifications.services.channels import SIGNATURE_HEADER, DeliveryError, sign
from apps.notifications.services.delivery import emit, send_test
from apps.notifications.services.dispatcher import Dispatcher
from apps.notifications.tests.conftest import FakeHttp
from apps.notifications.tests.factories import ChannelFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("commit")]

DOWN = {"server": "Germany", "host": "203.0.113.7", "last_seen": ""}
TELEGRAM_SEND = "https://api.telegram.org/bot999:panel/sendMessage"


def test_without_channels_nothing_happens(http: FakeHttp) -> None:
    assert emit(Event.SERVER_DOWN, DOWN) is None

    assert not Notification.objects.exists()
    assert http.posts == []


def test_an_event_goes_to_the_enabled_channels_routed_for_it(http: FakeHttp) -> None:
    routed = ChannelFactory(events=[Event.SERVER_DOWN])
    ChannelFactory(events=[Event.SERVER_DOWN], enabled=False)
    ChannelFactory(events=[Event.LIMIT_REACHED])

    emit(Event.SERVER_DOWN, DOWN)

    assert [post.url for post in http.posts] == [routed.target]
    assert Delivery.objects.get().status == DeliveryStatus.SENT


def test_the_webhook_body_is_signed_with_the_channel_secret(http: FakeHttp) -> None:
    channel = ChannelFactory(events=[Event.SERVER_DOWN], secret="s3cret")

    notification = emit(Event.SERVER_DOWN, DOWN)

    assert notification is not None
    [post] = http.posts
    assert post.headers[SIGNATURE_HEADER] == sign("s3cret", post.body)
    assert post.headers["X-Lamnezia-Event"] == "server_down"
    assert json.loads(post.body) == {
        "id": str(notification.uid),
        "event": "server_down",
        "created_at": notification.created_at.isoformat(),
        "data": DOWN,
    }
    assert channel.target == post.url


def test_a_repeated_occurrence_is_delivered_once(http: FakeHttp) -> None:
    ChannelFactory(events=[Event.SERVER_DOWN])

    first = emit(Event.SERVER_DOWN, DOWN, key="server_down:1:never")
    again = emit(Event.SERVER_DOWN, DOWN, key="server_down:1:never")

    assert first is not None
    assert again is None
    assert len(http.posts) == 1


def test_a_delivery_job_run_twice_sends_once(http: FakeHttp) -> None:
    ChannelFactory(events=[Event.SERVER_DOWN])
    emit(Event.SERVER_DOWN, DOWN)
    delivery = Delivery.objects.get()

    Dispatcher.from_settings().deliver(delivery.pk)

    assert len(http.posts) == 1
    delivery.refresh_from_db()
    assert (delivery.status, delivery.attempts) == (DeliveryStatus.SENT, 1)


def test_a_failing_channel_does_not_stop_the_others_and_shows_in_the_log(http: FakeHttp) -> None:
    broken = ChannelFactory(events=[Event.SERVER_DOWN])
    working = ChannelFactory(events=[Event.SERVER_DOWN])
    http.failing[broken.target] = DeliveryError("HTTP 404 not found", retryable=False)

    emit(Event.SERVER_DOWN, DOWN)

    assert [post.url for post in http.posts] == [working.target]
    failed = Delivery.objects.get(channel=broken)
    assert (failed.status, failed.attempts, failed.error) == (
        DeliveryStatus.FAILED,
        1,
        "HTTP 404 not found",
    )
    assert Delivery.objects.get(channel=working).status == DeliveryStatus.SENT


def test_a_temporary_failure_is_retried_until_the_attempts_run_out(http: FakeHttp) -> None:
    channel = ChannelFactory(events=[Event.SERVER_DOWN])
    http.failing[channel.target] = DeliveryError("HTTP 503", retryable=True)

    emit(Event.SERVER_DOWN, DOWN)

    delivery = Delivery.objects.get()
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.attempts == settings.NOTIFICATION_MAX_ATTEMPTS


def test_the_delay_between_attempts_doubles(http: FakeHttp) -> None:
    channel = ChannelFactory(events=[Event.SERVER_DOWN])
    emit(Event.SERVER_DOWN, DOWN)
    delivery = Delivery.objects.get()
    http.failing[channel.target] = DeliveryError("HTTP 503", retryable=True)
    dispatcher = Dispatcher.from_settings()
    delays = []
    for _attempt in range(3):
        Delivery.objects.filter(pk=delivery.pk).update(status=DeliveryStatus.PENDING)
        delays.append(dispatcher.deliver(delivery.pk))

    base = settings.NOTIFICATION_RETRY_DELAY
    assert delays == [base * 2, base * 4, base * 8]


def test_a_user_event_reaches_the_user_by_email(http: FakeHttp) -> None:
    user = UserFactory(email="alice@example.com")

    emit(Event.ACCESS_EXPIRING, {"user": user.username, "until": "2026-11-06"}, user=user)

    [letter] = mail.outbox
    assert letter.to == ["alice@example.com"]
    assert "2026-11-06" in letter.body
    assert Delivery.objects.get().to_user


def test_a_user_event_goes_to_the_telegram_the_user_chose(http: FakeHttp) -> None:
    configure_site(telegram_bot_token="999:panel")
    user = UserFactory(notify_via=NotifyVia.TELEGRAM)
    LinkedAccount.objects.create(user=user, messenger=Messenger.TELEGRAM, external_id="1001")

    emit(Event.ACCESS_EXPIRING, {"user": user.username, "until": "2026-11-06"}, user=user)

    [post] = http.posts
    assert post.url == "https://api.telegram.org/bot999:panel/sendMessage"
    assert json.loads(post.body)["chat_id"] == "1001"
    assert mail.outbox == []


def test_a_message_in_the_bot_chat_has_the_menu_button_in_the_users_language(
    http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tests have no compiled catalog: the label shows the language it was asked in.
    monkeypatch.setattr(personal, "_", lambda text: f"{text}:{translation.get_language()}")
    configure_site(telegram_bot_token="999:panel")
    user = UserFactory(notify_via=NotifyVia.TELEGRAM, language="ru")
    LinkedAccount.objects.create(user=user, messenger=Messenger.TELEGRAM, external_id="1001")

    emit(Event.ACCESS_EXPIRING, {"user": user.username, "until": "2026-11-06"}, user=user)

    [post] = http.posts
    keyboard = json.loads(post.body)["reply_markup"]["inline_keyboard"]
    assert keyboard == [[{"text": "Menu:ru", "callback_data": "menu"}]]


def test_a_message_to_an_administrators_channel_has_no_menu(http: FakeHttp) -> None:
    ChannelFactory(
        kind=ChannelKind.TELEGRAM, target="-100500", secret="123:own", events=[Event.SERVER_DOWN]
    )

    emit(Event.SERVER_DOWN, DOWN)

    [post] = http.posts
    assert "reply_markup" not in json.loads(post.body)


def test_a_message_to_a_bot_chat_opens_the_next_panel_below_it(http: FakeHttp) -> None:
    configure_site(telegram_bot_token="999:panel")
    user = UserFactory(email="alice@example.com", notify_via=NotifyVia.TELEGRAM)
    LinkedAccount.objects.create(user=user, messenger=Messenger.TELEGRAM, external_id="1001")
    panels = Panels()
    cache.clear()  # marks left by the other tests in this cache

    emit(Event.ACCESS_EXPIRING, {"user": user.username, "until": "2026-11-06"}, user=user)

    assert panels.take_buried("telegram:1001")
    assert not panels.take_buried("telegram:1001")


def test_an_email_or_a_failed_message_leaves_the_panel_alone(http: FakeHttp) -> None:
    configure_site(telegram_bot_token="999:panel")
    user = UserFactory(email="alice@example.com", notify_via=NotifyVia.TELEGRAM)
    LinkedAccount.objects.create(user=user, messenger=Messenger.TELEGRAM, external_id="1001")
    http.failing[TELEGRAM_SEND] = DeliveryError("Telegram is down", retryable=False)
    cache.clear()  # marks left by the other tests in this cache

    emit(Event.ACCESS_EXPIRING, {"user": user.username, "until": "2026-11-06"}, user=user)
    emit(Event.ACCESS_EXPIRING, {"user": "bob", "until": "2026-11-06"}, user=UserFactory())

    assert not Panels().take_buried("telegram:1001")


def test_the_messenger_of_the_event_wins_over_the_users_choice(http: FakeHttp) -> None:
    configure_site(telegram_bot_token="999:panel")
    user = UserFactory(email="", notify_via=NotifyVia.EMAIL)
    LinkedAccount.objects.create(user=user, messenger=Messenger.TELEGRAM, external_id="1001")

    emit(Event.CHAT_ANSWER, {"text": "Try again"}, user=user, via=Messenger.TELEGRAM)

    [post] = http.posts
    assert json.loads(post.body)["chat_id"] == "1001"
    assert "Try again" in json.loads(post.body)["text"]
    assert mail.outbox == []


def test_a_user_event_goes_to_the_discord_the_user_chose(http: FakeHttp) -> None:
    configure_site(discord_bot_token="panel-token")
    user = UserFactory(notify_via=NotifyVia.DISCORD)
    LinkedAccount.objects.create(user=user, messenger=Messenger.DISCORD, external_id="501")
    http.answers["https://discord.com/api/v10/users/@me/channels"] = b'{"id": "77"}'

    emit(Event.ACCESS_EXPIRING, {"user": user.username, "until": "2026-11-06"}, user=user)

    opened, posted = http.posts
    assert json.loads(opened.body) == {"recipient_id": "501"}
    body = json.loads(posted.body)
    assert "2026-11-06" in body["content"]
    (row,) = body["components"]
    assert [(b["label"], b["custom_id"]) for b in row["components"]] == [("Menu", "lamnezia:menu")]
    assert mail.outbox == []


def test_discord_without_the_bot_token_falls_back_to_email(http: FakeHttp) -> None:
    user = UserFactory(notify_via=NotifyVia.DISCORD)
    LinkedAccount.objects.create(user=user, messenger=Messenger.DISCORD, external_id="501")

    emit(Event.ACCESS_EXPIRING, {"user": user.username, "until": "2026-11-06"}, user=user)

    assert http.posts == []
    assert len(mail.outbox) == 1


def test_a_messenger_choice_without_a_link_falls_back_to_email(http: FakeHttp) -> None:
    configure_site(telegram_bot_token="999:panel")
    user = UserFactory(notify_via=NotifyVia.TELEGRAM)

    emit(Event.ACCESS_EXPIRING, {"user": user.username, "until": "2026-11-06"}, user=user)

    assert http.posts == []
    assert len(mail.outbox) == 1


def test_an_admin_event_is_not_sent_to_the_user(http: FakeHttp) -> None:
    user = UserFactory()

    assert emit(Event.REGISTRATION_PENDING, {"user": user.username}, user=user) is None
    assert mail.outbox == []


def test_email_channel_sends_to_its_addresses(http: FakeHttp) -> None:
    ChannelFactory(
        kind=ChannelKind.EMAIL, target="a@example.com, b@example.com", events=[Event.SERVER_DOWN]
    )

    emit(Event.SERVER_DOWN, DOWN)

    [letter] = mail.outbox
    assert letter.to == ["a@example.com", "b@example.com"]
    assert "Germany" in letter.subject
    assert "203.0.113.7" in letter.body


def test_a_test_message_reaches_even_a_disabled_channel(http: FakeHttp) -> None:
    channel = ChannelFactory(enabled=False)

    delivery = send_test(channel)

    delivery.refresh_from_db()
    assert delivery.status == DeliveryStatus.SENT
    assert json.loads(http.posts[0].body)["event"] == "test"


def test_a_deleted_channel_fails_its_waiting_deliveries(http: FakeHttp) -> None:
    channel = ChannelFactory(events=[Event.SERVER_DOWN])
    http.failing[channel.target] = DeliveryError("HTTP 503", retryable=True)
    emit(Event.SERVER_DOWN, DOWN)
    delivery = Delivery.objects.get()
    Delivery.objects.filter(pk=delivery.pk).update(status=DeliveryStatus.PENDING)
    channel.delete()

    Dispatcher.from_settings().deliver(delivery.pk)

    delivery.refresh_from_db()
    assert (delivery.status, delivery.error) == (DeliveryStatus.FAILED, "the channel is deleted")
    assert delivery.channel_name == str(channel)
