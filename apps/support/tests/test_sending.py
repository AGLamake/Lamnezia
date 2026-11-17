"""A message sent again with the same token (a retry of the chat) is saved once."""

import pytest
from django.test import Client
from django.urls import reverse

from apps.support.models import Message, TicketType
from apps.support.services.chat import MyChat
from apps.support.services.sending import SendOnce
from apps.support.services.tickets import MyRequests
from apps.support.tests.test_inbox import admin, request_of
from apps.support.tests.test_tickets import subscriber

pytestmark = pytest.mark.django_db


class TestSendOnce:
    def test_a_token_is_claimed_once(self) -> None:
        user = subscriber()

        with SendOnce(user, "abc") as first, SendOnce(user, "abc") as second:
            pass

        assert (first, second) == (True, False)

    def test_tokens_are_kept_apart_per_user(self) -> None:
        with SendOnce(subscriber(), "abc") as first, SendOnce(admin(), "abc") as second:
            pass

        assert (first, second) == (True, True)

    def test_a_failed_send_gives_the_token_back(self) -> None:
        user = subscriber()

        with pytest.raises(RuntimeError), SendOnce(user, "abc"):
            raise RuntimeError

        with SendOnce(user, "abc") as again:
            assert again

    def test_a_duplicate_that_fails_keeps_the_first_claim(self) -> None:
        user = subscriber()
        with SendOnce(user, "abc"):
            pass

        with pytest.raises(RuntimeError), SendOnce(user, "abc"):
            raise RuntimeError

        with SendOnce(user, "abc") as again:
            assert not again

    def test_without_a_token_every_send_goes(self) -> None:
        user = subscriber()

        with SendOnce(user, "") as first, SendOnce(user, "") as second:
            pass

        assert (first, second) == (True, True)


def test_a_chat_message_sent_twice_with_one_token_is_saved_once(client: Client) -> None:
    user = subscriber()
    client.force_login(user)

    for _ in range(2):
        response = client.post(
            reverse("support:chat"), {"text": "Hi", "token": "t1"}, HTTP_HX_REQUEST="true"
        )

    chat = MyChat(user).get()
    assert chat is not None
    assert chat.messages.filter(text="Hi").count() == 1
    # The retry gets the thread with the message, so the page shows it as sent.
    assert "Hi" in response.content.decode()


def test_a_reply_to_a_request_sent_twice_with_one_token_is_saved_once(client: Client) -> None:
    conversation = MyRequests(subscriber()).open(TicketType.QUESTION, "Hello")
    client.force_login(conversation.user)
    url = reverse("support:conversation", args=[conversation.pk])

    for token in ("t1", "t1", "t2"):
        client.post(url, {"text": "More", "token": token}, HTTP_HX_REQUEST="true")

    assert conversation.messages.filter(text="More").count() == 2


def test_a_staff_reply_sent_twice_with_one_token_is_saved_once(client: Client) -> None:
    conversation = request_of(subscriber())
    client.force_login(admin())
    url = reverse("support:inbox-conversation", args=[conversation.pk])

    for _ in range(2):
        client.post(url, {"text": "Restart it", "token": "t1"}, HTTP_HX_REQUEST="true")

    assert Message.objects.filter(conversation=conversation, text="Restart it").count() == 1


def test_the_staff_thread_has_a_pending_card_of_the_staff(client: Client) -> None:
    conversation = request_of(subscriber())
    member = admin()
    client.force_login(member)

    page = client.get(reverse("support:inbox-conversation", args=[conversation.pk])).content
    page_text = page.decode()

    assert "data-chat-send" in page_text
    assert "data-chat-messages" in page_text
    assert 'data-internal-class="border-warning bg-warning-soft"' in page_text
    assert member.username in page_text
