"""The chat widget of the portal and the lines about requests in the chat."""

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.servers.tests.factories import ServerFactory
from apps.support.models import Message, TicketEvent, TicketStatus
from apps.support.services.chat import MyChat
from apps.support.services.inbox import Inbox
from apps.support.services.incidents import close_incident
from apps.support.services.thread import MyThreads
from apps.support.services.tickets import MyRequests
from apps.support.tests.test_inbox import admin, request_of
from apps.support.tests.test_tickets import subscriber

pytestmark = pytest.mark.django_db


def chat_lines(user: User) -> list[tuple[str, int | None]]:
    chat = MyChat(user).get()
    assert chat is not None
    return [(line.event, line.ticket_id) for line in chat.messages.exclude(event="")]


class TestRequestEvents:
    def test_an_answer_to_a_request_is_a_line_in_the_chat_without_its_text(self) -> None:
        user = subscriber()
        ticket = request_of(user)

        Inbox(admin()).reply(ticket, "Reinstall the app")

        line = Message.objects.get(event=TicketEvent.ANSWER)
        assert line.conversation == MyChat(user).get()
        assert (line.ticket, line.author, line.text) == (ticket, None, "")

    def test_a_line_is_seen_at_once_and_adds_nothing_to_the_counters(self) -> None:
        user = subscriber()
        ticket = request_of(user)

        Inbox(admin()).reply(ticket, "Reinstall the app")

        assert MyChat(user).unread() == 0
        assert MyRequests(user).unread() == 1

    def test_the_statuses_the_user_hears_of_are_lines_too(self) -> None:
        user = subscriber()
        ticket = request_of(user)
        staff = Inbox(admin())

        staff.set_status(ticket, TicketStatus.IN_PROGRESS)
        staff.set_status(ticket, TicketStatus.WAITING_USER)
        staff.set_status(ticket, TicketStatus.WAITING_USER)
        staff.set_status(ticket, TicketStatus.CLOSED)

        assert chat_lines(user) == [
            (TicketEvent.WAITING_USER, ticket.pk),
            (TicketEvent.CLOSED, ticket.pk),
        ]

    def test_notes_and_chat_answers_add_no_lines(self) -> None:
        user = subscriber()
        ticket = request_of(user)
        MyChat(user).send("Hello")
        staff = Inbox(admin())

        staff.note(ticket, "Looks like the provider")
        chat = MyChat(user).get()
        assert chat is not None
        staff.reply(chat, "Hi!")

        assert chat_lines(user) == []

    def test_closing_an_incident_tells_each_reporter_in_the_chat(self) -> None:
        server = ServerFactory()
        user = subscriber(server)
        ticket = MyRequests(user).report_server(server)
        assert ticket.incident is not None

        close_incident(ticket.incident, admin(), "Works again")

        assert chat_lines(user) == [
            (TicketEvent.ANSWER, ticket.pk),
            (TicketEvent.CLOSED, ticket.pk),
        ]

    def test_a_chat_of_lines_alone_is_not_in_the_inbox(self) -> None:
        user = subscriber()
        staff = Inbox(admin())
        staff.reply(request_of(user), "Done")

        assert not staff.chats().exists()
        MyChat(user).send("Thanks")
        assert staff.chats().count() == 1

    def test_lines_do_not_move_the_chat_up_in_the_inbox(self) -> None:
        user = subscriber()
        MyChat(user).send("Hello")
        chat = MyChat(user).get()
        assert chat is not None

        Inbox(admin()).reply(request_of(user), "Done")

        chat_after = MyChat(user).get()
        assert chat_after is not None
        assert chat_after.updated_at == chat.updated_at


class TestCounters:
    def test_the_widget_counts_chat_answers_and_the_tab_only_requests(self, client: Client) -> None:
        user = subscriber()
        MyChat(user).send("Hello")
        chat = MyChat(user).get()
        assert chat is not None
        staff = Inbox(admin())
        staff.reply(chat, "Hi!")
        staff.reply(chat, "How can we help?")
        staff.reply(request_of(user), "Done")
        client.force_login(user)

        assert (MyChat(user).unread(), MyRequests(user).unread(), MyThreads(user).unread()) == (
            2,
            1,
            3,
        )
        assert ">2<" in client.get(reverse("support:chat-unread")).content.decode()
        assert ">1<" in client.get(reverse("support:unread")).content.decode()


class TestWidget:
    def test_every_portal_page_has_the_chat_button(self, client: Client) -> None:
        client.force_login(subscriber())

        for name in ("portal:index", "portal:servers", "support:index", "guides:index"):
            page = client.get(reverse(name)).content.decode()
            assert 'data-dialog-open="support-chat"' in page, name
            assert 'id="support-chat"' in page, name

    def test_the_counter_on_the_chat_button_refreshes_itself_not_the_chat(
        self, client: Client
    ) -> None:
        client.force_login(subscriber())

        page = client.get(reverse("portal:index")).content.decode()

        badge = f'hx-get="{reverse("support:chat-unread")}" hx-target="this"'
        assert badge in page

    def test_the_support_tab_holds_requests_only(self, client: Client) -> None:
        client.force_login(subscriber())

        page = client.get(reverse("support:index")).content.decode()

        assert reverse("support:new") in page
        assert f'href="{reverse("support:chat")}"' not in page

    def test_the_old_chat_address_opens_the_widget_on_the_portal(self, client: Client) -> None:
        client.force_login(subscriber())

        response = client.get(reverse("support:chat"))

        assert response.status_code == 302
        assert response["Location"] == reverse("portal:index") + "#support-chat"

    def test_opening_the_widget_shows_the_chat_and_marks_answers_seen(self, client: Client) -> None:
        user = subscriber()
        MyChat(user).send("Hello")
        chat = MyChat(user).get()
        assert chat is not None
        Inbox(admin()).reply(chat, "Hi there")
        client.force_login(user)

        page = client.get(reverse("support:chat"), HTTP_HX_REQUEST="true").content.decode()

        assert "Hi there" in page
        assert 'id="chat-thread"' in page
        assert 'id="chat-messages"' in page
        # Chat signals come over the user's socket: the page keeps following its own thread.
        assert "data-live-thread" not in page
        # Opened again, the widget refetches the messages only.
        assert "data-dialog-refresh" in page
        assert 'hx-trigger="support:thread from:body, dialog:refresh"' in page
        assert MyChat(user).unread() == 0

    def test_a_message_sent_in_the_widget_gets_the_widget_thread_back(self, client: Client) -> None:
        user = subscriber()
        client.force_login(user)

        response = client.post(reverse("support:chat"), {"text": "Hi"}, HTTP_HX_REQUEST="true")

        page = response.content.decode()
        assert 'hx-target="#chat-thread"' in page
        assert "Hi" in page

    def test_a_request_event_shows_as_a_line_with_a_link(self, client: Client) -> None:
        user = subscriber()
        ticket = request_of(user)
        staff = Inbox(admin())
        staff.reply(ticket, "Reinstall the app")
        staff.set_status(ticket, TicketStatus.CLOSED)
        client.force_login(user)

        page = client.get(reverse("support:chat"), HTTP_HX_REQUEST="true").content.decode()

        link = reverse("support:conversation", args=[ticket.pk])
        assert page.count(f'href="{link}"') == 2
        assert f"Request #{ticket.pk}: support answered" in page
        assert f"Request #{ticket.pk}: closed" in page
        assert "Reinstall the app" not in page

    def test_refetching_the_chat_keeps_the_widget_ids(self, client: Client) -> None:
        user = subscriber()
        MyChat(user).send("Hello")
        chat = MyChat(user).get()
        assert chat is not None
        client.force_login(user)

        page = client.get(reverse("support:messages", args=[chat.pk])).content.decode()

        assert 'id="chat-messages"' in page
        assert "data-live-thread" not in page

    def test_the_staff_see_request_lines_in_the_chat(self, client: Client) -> None:
        user = subscriber()
        MyChat(user).send("Hello")
        chat = MyChat(user).get()
        assert chat is not None
        member = admin()
        ticket = request_of(user)
        Inbox(member).reply(ticket, "Reinstall the app")
        client.force_login(member)

        page = client.get(reverse("support:inbox-messages", args=[chat.pk])).content.decode()

        assert f"Request #{ticket.pk}: support answered" in page
        assert reverse("support:inbox-conversation", args=[ticket.pk]) in page
