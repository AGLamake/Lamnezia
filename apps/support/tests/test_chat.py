"""The user's chat, the message rate limit and read marks."""

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.notifications.events import Event
from apps.notifications.models import Channel, ChannelKind, Notification, Route
from apps.support.models import Conversation, Kind
from apps.support.services.chat import MyChat
from apps.support.services.inbox import Inbox
from apps.support.services.thread import MyThreads, TooManyMessagesError
from apps.support.tests.test_inbox import admin, request_of
from apps.support.tests.test_tickets import subscriber

pytestmark = pytest.mark.django_db


class TestMyChat:
    def test_the_first_message_opens_the_one_chat_of_the_user(self) -> None:
        user = subscriber()
        chat = MyChat(user)

        assert chat.get() is None
        chat.send("Hello")
        chat.send("Anyone?")

        conversation = Conversation.objects.get(user=user)
        assert conversation.kind == Kind.CHAT
        assert conversation.messages.count() == 2

    def test_the_routed_channels_hear_of_a_chat_message(self) -> None:
        channel = Channel.objects.create(name="Ops", kind=ChannelKind.WEBHOOK, target="https://x")
        Route.objects.create(channel=channel, event=Event.CHAT_MESSAGE)
        user = subscriber()

        message = MyChat(user).send("The key does not import")

        notification = Notification.objects.get(event=Event.CHAT_MESSAGE)
        assert notification.data == {
            "user": user.username,
            "number": message.conversation_id,
            "text": "The key does not import",
        }

    @override_settings(SUPPORT_MESSAGE_RATE_LIMIT=3)
    def test_a_user_sends_at_most_the_limit_of_messages_a_minute(self) -> None:
        user = subscriber()
        conversation = request_of(user)  # the text of a request is a message too
        MyChat(user).send("one")
        MyChat(user).send("two")

        with pytest.raises(TooManyMessagesError):
            MyChat(user).send("three")
        with pytest.raises(TooManyMessagesError):
            MyThreads(user)._post(conversation, "four")


class TestReadMarks:
    def test_each_side_marks_what_the_other_wrote(self) -> None:
        user = subscriber()
        staff = Inbox(admin())
        conversation = request_of(user, "Broken")
        staff.note(conversation, "Internal")
        answer = staff.reply(conversation, "Try again")

        assert MyThreads(user).unread() == 1
        MyThreads(user).mark_read(conversation)
        staff.mark_read(conversation)

        answer.refresh_from_db()
        assert answer.read_at is not None
        assert MyThreads(user).unread() == 0
        assert staff.unread() == 0
        assert conversation.messages.get(internal=True).read_at is None


class TestPages:
    def test_the_chat_page_sends_and_shows_messages(self, client: Client) -> None:
        user = subscriber()
        client.force_login(user)

        response = client.post(reverse("support:chat"), {"text": "Hi"}, HTTP_HX_REQUEST="true")

        conversation = MyChat(user).get()
        assert conversation is not None
        assert 'id="chat-thread"' in response.content.decode()
        widget = client.get(reverse("support:chat"), HTTP_HX_REQUEST="true")
        assert "Hi" in widget.content.decode()

    @override_settings(SUPPORT_MESSAGE_RATE_LIMIT=1)
    def test_a_message_over_the_limit_keeps_its_text(self, client: Client) -> None:
        client.force_login(subscriber())
        client.post(reverse("support:chat"), {"text": "one"})

        response = client.post(reverse("support:chat"), {"text": "two"}, HTTP_HX_REQUEST="true")

        assert "Wait a minute" in response.content.decode()
        assert "two</textarea>" in response.content.decode()

    def test_polling_gets_new_answers_and_marks_them_seen(self, client: Client) -> None:
        user = subscriber()
        conversation = request_of(user)
        answer = Inbox(admin()).reply(conversation, "We are on it")
        client.force_login(user)

        assert ">1<" in client.get(reverse("support:unread")).content.decode()
        response = client.get(reverse("support:messages", args=[conversation.pk]))

        assert "We are on it" in response.content.decode()
        answer.refresh_from_db()
        assert answer.read_at is not None

    def test_the_messages_of_someone_elses_thread_are_not_found(self, client: Client) -> None:
        conversation = request_of(subscriber())
        client.force_login(subscriber())

        response = client.get(reverse("support:messages", args=[conversation.pk]))

        assert response.status_code == 404

    def test_the_staff_poll_their_thread_without_the_users_side(self, client: Client) -> None:
        user = subscriber()
        conversation = request_of(user, "Help")
        Inbox(admin()).note(conversation, "Secret note")
        client.force_login(UserFactory(role=Role.ADMIN))

        staff_view = client.get(reverse("support:inbox-messages", args=[conversation.pk]))
        client.force_login(user)
        user_view = client.get(reverse("support:messages", args=[conversation.pk]))

        assert "Secret note" in staff_view.content.decode()
        assert "Secret note" not in user_view.content.decode()
        assert client.get(reverse("support:inbox-unread")).status_code in (302, 403)
