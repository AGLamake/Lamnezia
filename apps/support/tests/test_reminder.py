"""The reminder of an answer the user neither answered nor read, in their own channel."""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from django.conf import settings
from django.core import mail
from django.db import transaction

from apps.accounts.models import LinkedAccount, Messenger, NotifyVia, User
from apps.accounts.tests.registration import configure as configure_site
from apps.notifications.events import EVENTS, Event
from apps.notifications.models import Notification
from apps.notifications.services.channels import DeliveryError, HttpClient
from apps.notifications.tests.conftest import FakeHttp
from apps.support import tasks
from apps.support.models import Conversation, Message
from apps.support.services.chat import MyChat
from apps.support.services.inbox import Inbox
from apps.support.services.thread import MyThreads
from apps.support.services.tickets import MyRequests
from apps.support.tests.test_inbox import admin, request_of
from apps.support.tests.test_tickets import subscriber

pytestmark = pytest.mark.django_db

Answer = Callable[[Conversation, str], list[Callable[[], Any]]]
TELEGRAM_SEND = "https://api.telegram.org/bot999:panel/sendMessage"
DISCORD_OPEN = "https://discord.com/api/v10/users/@me/channels"


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeHttp]:
    fake = FakeHttp()
    monkeypatch.setattr(HttpClient, "post", lambda self, *args, **kw: fake.post(*args, **kw))
    yield fake


@pytest.fixture
def answer(monkeypatch: pytest.MonkeyPatch, http: FakeHttp) -> Answer:
    """The staff answer, delivered at once; the returned reminder jobs wait for the test to say
    the delay is over.
    """
    staff = Inbox(admin())
    monkeypatch.setattr(transaction, "on_commit", lambda job: job())
    queued: list[Callable[[], Any]] = []

    def later(args: tuple[Any, ...], countdown: int) -> None:
        queued.append(lambda: tasks.remind_of_answer(*args))

    monkeypatch.setattr(tasks.remind_of_answer, "apply_async", later)

    def reply(conversation: Conversation, text: str) -> list[Callable[[], Any]]:
        queued.clear()
        staff.reply(conversation, text)
        return list(queued)

    return reply


def on_telegram(notify_via: str = NotifyVia.TELEGRAM) -> User:
    """A user who wrote to the chat from Telegram."""
    configure_site(telegram_bot_token="999:panel", discord_bot_token="panel-token")
    user = subscriber()
    user.notify_via = notify_via
    user.save()
    LinkedAccount.objects.create(user=user, messenger=Messenger.TELEGRAM, external_id="1001")
    MyChat(user, Messenger.TELEGRAM).send("Hello")
    return user


def delay_is_over(*jobs: list[Callable[[], Any]]) -> None:
    for callbacks in jobs:
        for callback in callbacks:
            callback()


def reminders(user: User) -> list[Notification]:
    events = (Event.SUPPORT_ANSWER, Event.REQUEST_REMINDER)
    return list(Notification.objects.filter(event__in=events, user=user))


class TestReminder:
    def test_an_answer_left_alone_brings_one_reminder_with_a_link(self, answer: Answer) -> None:
        user = subscriber()
        conversation = request_of(user, "No handshake")

        delay_is_over(answer(conversation, "Reinstall the app"))

        [reminder] = reminders(user)
        assert reminder.data["topic"] == "No handshake"
        assert reminder.data["url"].endswith(f"/support/{conversation.pk}/")
        assert "Reinstall the app" not in str(reminder.data)

    def test_a_chat_answer_links_to_the_chat(self, answer: Answer) -> None:
        user = subscriber()
        MyChat(user).send("Hello")

        delay_is_over(answer(user.conversations.get(), "Hi"))

        [reminder] = reminders(user)
        assert reminder.data["url"].endswith("/support/chat/")

    def test_the_check_waits_two_minutes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        queued: list[dict[str, Any]] = []
        monkeypatch.setattr(transaction, "on_commit", lambda job: job())
        monkeypatch.setattr(
            tasks.remind_of_answer, "apply_async", lambda *a, **kw: queued.append(kw)
        )

        Inbox(admin()).reply(request_of(subscriber()), "Done")

        assert queued == [{"countdown": 120}]
        assert settings.SUPPORT_REMINDER_DELAY.total_seconds() == 120

    def test_a_user_who_answered_meanwhile_is_not_reminded(self, answer: Answer) -> None:
        user = subscriber()
        conversation = request_of(user)

        later = answer(conversation, "Try again")
        MyRequests(user).reply(conversation, "Works now")
        delay_is_over(later)

        assert reminders(user) == []

    def test_a_user_who_read_the_answer_is_not_reminded(self, answer: Answer) -> None:
        user = subscriber()
        conversation = request_of(user)

        later = answer(conversation, "Try again")
        MyThreads(user).mark_read(conversation)
        delay_is_over(later)

        assert reminders(user) == []

    def test_answers_in_a_row_make_one_reminder_until_the_user_writes(self, answer: Answer) -> None:
        user = subscriber()
        MyChat(user).send("Hello")
        conversation = user.conversations.get()

        delay_is_over(answer(conversation, "First"), answer(conversation, "Second"))
        assert len(reminders(user)) == 1

        MyChat(user).send("And now?")
        delay_is_over(answer(conversation, "Third"))
        assert len(reminders(user)) == 2

    def test_an_answer_that_went_to_the_chosen_messenger_is_not_repeated(
        self, answer: Answer, http: FakeHttp
    ) -> None:
        user = on_telegram()

        delay_is_over(answer(user.conversations.get(), "Hi"))

        assert Notification.objects.get(event=Event.CHAT_ANSWER).sent_via == NotifyVia.TELEGRAM
        assert reminders(user) == []
        assert len(http.to(TELEGRAM_SEND)) == 1

    def test_an_answer_in_a_messenger_is_reminded_by_the_chosen_email(
        self, answer: Answer, http: FakeHttp
    ) -> None:
        user = on_telegram(NotifyVia.EMAIL)

        delay_is_over(answer(user.conversations.get(), "Hi"))

        assert len(reminders(user)) == 1
        assert len(http.to(TELEGRAM_SEND)) == 1
        [letter] = mail.outbox
        assert letter.to == [user.email]

    def test_an_answer_the_blocked_bot_did_not_take_is_reminded(
        self, answer: Answer, http: FakeHttp
    ) -> None:
        user = on_telegram()
        http.failing[TELEGRAM_SEND] = DeliveryError("HTTP 403: bot was blocked", retryable=False)
        http.answers[DISCORD_OPEN] = b'{"id": "77"}'
        LinkedAccount.objects.create(user=user, messenger=Messenger.DISCORD, external_id="501")

        delay_is_over(answer(user.conversations.get(), "Hi"))

        [reminder] = reminders(user)
        assert reminder.skip_via == ""
        assert reminder.sent_via == NotifyVia.DISCORD

    def test_the_reminder_leaves_out_the_channel_the_answer_reached(
        self, answer: Answer, http: FakeHttp
    ) -> None:
        user = on_telegram(NotifyVia.DISCORD)
        http.failing[DISCORD_OPEN] = DeliveryError("Discord refused", retryable=False)

        delay_is_over(answer(user.conversations.get(), "Hi"))

        [reminder] = reminders(user)
        assert reminder.skip_via == NotifyVia.TELEGRAM
        assert len(http.to(TELEGRAM_SEND)) == 1  # the answer only
        assert reminder.sent_via == NotifyVia.EMAIL

    def test_a_chat_reminder_names_the_chat_and_a_request_reminder_its_topic(
        self, answer: Answer
    ) -> None:
        user = subscriber()
        MyChat(user).send("Hello")
        delay_is_over(answer(user.conversations.get(), "Hi"))
        delay_is_over(answer(request_of(user, "No handshake"), "Reinstall"))

        chat = Notification.objects.get(event=Event.SUPPORT_ANSWER)
        request = Notification.objects.get(event=Event.REQUEST_REMINDER)
        _, chat_text = EVENTS[Event.SUPPORT_ANSWER].render(chat.data)
        _, request_text = EVENTS[Event.REQUEST_REMINDER].render(request.data)
        assert chat_text.startswith("There is a new message in your support chat.\n")
        assert "—" not in chat_text
        assert "There is a new answer to your request: No handshake." in request_text

    def test_a_deleted_answer_is_skipped(self) -> None:
        tasks.remind_of_answer(Message.objects.count() + 1)

        assert not Notification.objects.exists()
