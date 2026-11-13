from pathlib import Path
from typing import Any

import pytest

from apps.accounts.models import Messenger, User
from apps.bots.services.menu import MENU
from apps.bots.services.replies import Attachment
from apps.bots.tests.conftest import link
from apps.bots.tests.fake_adapter import FakeAdapter
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.portal.tests.test_portal import subscriber
from apps.support.models import Attachment as Image
from apps.support.models import Conversation, Kind, TicketStatus, TicketType
from apps.support.services.inbox import Inbox
from apps.support.tests.test_inbox import admin

pytestmark = pytest.mark.django_db


def test_a_message_from_the_bot_is_in_the_staffs_inbox(bot: FakeAdapter, user: User) -> None:
    bot.say("The key does not import")

    (chat,) = Inbox(admin()).chats()
    assert chat.user == user
    assert chat.messages.get().via == Messenger.TELEGRAM


def test_the_staffs_answer_goes_back_to_the_bot(bot: FakeAdapter, user: User) -> None:
    bot.say("The key does not import")

    Inbox(admin()).reply(Conversation.objects.get(), "Import it again")

    notification = Notification.objects.get(event=Event.CHAT_ANSWER)
    assert (notification.user, notification.via) == (user, Messenger.TELEGRAM)


def test_open_a_request(bot: FakeAdapter, user: User) -> None:
    types = bot.say("/ticket")
    asking = bot.press_button(types, str(TicketType.QUESTION.label))

    opened = bot.say("Which app for a TV?\nI have an Android TV.")

    assert "one message" in asking.text
    conversation = Conversation.objects.get(kind=Kind.TICKET)
    assert f"#{conversation.pk}" in opened.text
    assert (conversation.type, conversation.subject) == (TicketType.QUESTION, "Which app for a TV?")
    assert conversation.messages.get().via == Messenger.TELEGRAM


def test_the_request_types_include_a_server_down(bot: FakeAdapter, user: User) -> None:
    actions = [button.action for button in bot.say("/ticket").buttons]

    assert "down" in actions
    assert "ticket bug" in actions


def test_list_read_and_answer_a_request(bot: FakeAdapter, user: User) -> None:
    bot.press("ticket bug")
    bot.say("No handshake")
    conversation = Conversation.objects.get()
    Inbox(admin()).reply(conversation, "Which server?")

    listed = bot.say("/requests")
    shown = bot.press(f"request {conversation.pk}")
    bot.press_button(shown, "Answer")
    sent = bot.say("Amsterdam")

    assert "No handshake" in listed.text
    assert "You: No handshake" in shown.text and "Support: Which server?" in shown.text
    assert sent.text == "Sent to the administrators."
    assert conversation.messages.filter(author=user).count() == 2


def test_no_requests_yet(bot: FakeAdapter, user: User) -> None:
    assert bot.say("/requests").text == "You have no requests yet."


def test_a_closed_request_takes_no_answer(bot: FakeAdapter, user: User) -> None:
    bot.press("ticket bug")
    bot.say("No handshake")
    conversation = Conversation.objects.get()
    Inbox(admin()).set_status(conversation, TicketStatus.CLOSED)

    assert "Answer" not in [b.label for b in bot.press(f"request {conversation.pk}").buttons]
    bot.press(f"answer {conversation.pk}")
    assert "closed" in bot.say("Still broken").text


def test_someone_elses_request_is_not_shown(bot: FakeAdapter, user: User) -> None:
    other = FakeAdapter("2002", bot.core)
    link(subscriber(max_keys=1), "2002")
    other.press("ticket bug")
    other.say("Not yours")

    assert "not available" in bot.press(f"request {Conversation.objects.get().pk}").text


def test_the_menu_lists_requests() -> None:
    names = {command.name for command in MENU}

    assert {"ticket", "requests"} <= names


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


@pytest.fixture
def _media(settings: Any, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = tmp_path


@pytest.mark.usefixtures("_media")
class TestPictures:
    def test_a_picture_goes_to_the_chat_with_its_caption(
        self, bot: FakeAdapter, user: User
    ) -> None:
        reply = bot.send_picture(Attachment("shot.png", PNG, "image/png"), "The error")

        assert "Sent to the administrators" in reply.text
        message = Conversation.objects.get(kind=Kind.CHAT).messages.get()
        assert message.text == "The error"
        assert message.attachments.get().content_type == "image/png"

    def test_a_picture_without_a_caption(self, bot: FakeAdapter, user: User) -> None:
        bot.send_picture(Attachment("shot.png", PNG, "image/png"))

        assert Image.objects.count() == 1

    def test_a_picture_answers_a_request(self, bot: FakeAdapter, user: User) -> None:
        bot.press("ticket bug")
        bot.say("Nothing works")
        conversation = Conversation.objects.get(kind=Kind.TICKET)
        bot.press(f"answer {conversation.pk}")

        bot.send_picture(Attachment("shot.png", PNG, "image/png"))

        assert conversation.messages.last().attachments.count() == 1  # type: ignore[union-attr]

    def test_not_an_image_is_refused(self, bot: FakeAdapter, user: User) -> None:
        reply = bot.send_picture(Attachment("shot.png", b"not an image", "image/png"))

        assert "PNG, JPEG, GIF or WebP" in reply.text
        assert not Conversation.objects.filter(kind=Kind.CHAT, messages__isnull=False).exists()
