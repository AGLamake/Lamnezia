import pytest
from django.test import Client
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.servers.tests.factories import ServerFactory
from apps.support.models import Conversation, Incident, Kind, ReplyTemplate, TicketStatus
from apps.support.services.inbox import Inbox
from apps.support.services.thread import post
from apps.support.services.tickets import MyRequests
from apps.support.tests.test_inbox import admin, moderator, request_of, served
from apps.support.tests.test_tickets import subscriber

pytestmark = pytest.mark.django_db


def test_the_menu_counts_unread_messages(client: Client) -> None:
    request_of(subscriber())
    client.force_login(admin())

    page = client.get(reverse("support:inbox"))

    assert page.status_code == 200
    assert b'nav-count nav-count-danger">1<' in page.content


def test_a_moderator_does_not_open_a_request_not_assigned_to_them(client: Client) -> None:
    conversation = request_of(subscriber())
    client.force_login(moderator())

    page = client.get(reverse("support:inbox-conversation", args=[conversation.pk]))

    assert page.status_code == 404


def test_a_user_does_not_open_the_inbox(client: Client) -> None:
    client.force_login(subscriber())

    assert client.get(reverse("support:inbox")).status_code == 403


def test_opening_a_request_reads_it_and_offers_templates(client: Client) -> None:
    staff = admin()
    conversation = request_of(subscriber())
    ReplyTemplate.objects.create(title="Restart", text="Restart the app, please.")
    client.force_login(staff)

    page = client.get(reverse("support:inbox-conversation", args=[conversation.pk]))

    assert b"Restart the app, please." in page.content
    assert Inbox(staff).unread() == 0


def test_an_internal_note_is_saved_hidden(client: Client) -> None:
    user = subscriber()
    conversation = request_of(user)
    client.force_login(admin())

    client.post(
        reverse("support:inbox-conversation", args=[conversation.pk]),
        {"text": "Called them", "internal": "on"},
    )

    assert conversation.messages.get(text="Called them").internal
    assert conversation.messages.count() == 2


def test_an_admin_assigns_and_a_moderator_changes_the_status(client: Client) -> None:
    member = moderator()
    conversation = request_of(served(subscriber(), member))
    manage = reverse("support:inbox-manage", args=[conversation.pk])
    client.force_login(admin())
    client.post(manage, {"status": TicketStatus.IN_PROGRESS, "assignee": member.pk})

    client.force_login(member)
    client.post(manage, {"status": TicketStatus.RESOLVED, "assignee": ""})

    conversation.refresh_from_db()
    assert conversation.status == TicketStatus.RESOLVED
    assert conversation.assignee == member


def test_closing_an_incident_from_its_page(client: Client) -> None:
    server = ServerFactory()
    MyRequests(subscriber(server)).report_server(server)
    incident = Incident.objects.get()
    client.force_login(admin())
    assert b"1 person reported" in client.get(reverse("support:incidents")).content

    response = client.post(reverse("support:incident-close", args=[incident.pk]), {"text": ""})

    assert response["Location"] == reverse("support:incidents")
    incident.refresh_from_db()
    assert not incident.is_open


def test_a_moderator_does_not_close_incidents(client: Client) -> None:
    client.force_login(moderator())

    assert client.get(reverse("support:incidents")).status_code == 403


class TestReplyTemplates:
    def test_an_admin_adds_edits_and_deletes_a_template(self, client: Client) -> None:
        client.force_login(admin())

        client.post(reverse("support:template-add"), {"title": "Restart", "text": "Restart it."})
        template = ReplyTemplate.objects.get()
        client.post(
            reverse("support:template-edit", args=[template.pk]),
            {"title": "Restart", "text": "Restart the app."},
        )
        template.refresh_from_db()
        listing = client.get(reverse("support:templates"))
        client.post(reverse("support:template-delete", args=[template.pk]))

        assert template.text == "Restart the app."
        assert b"Restart the app." in listing.content
        assert not ReplyTemplate.objects.exists()
        assert list(AuditEvent.objects.order_by("pk").values_list("action", flat=True)) == [
            "support.template_create",
            "support.template_update",
            "support.template_delete",
        ]

    def test_the_inbox_shows_the_tab_to_admins_only(self, client: Client) -> None:
        templates = reverse("support:templates").encode()
        client.force_login(admin())
        for_admin = client.get(reverse("support:inbox"))
        client.force_login(moderator())
        for_moderator = client.get(reverse("support:inbox"))

        assert templates in for_admin.content
        assert templates not in for_moderator.content

    def test_a_moderator_does_not_edit_templates(self, client: Client) -> None:
        template = ReplyTemplate.objects.create(title="Restart", text="Restart it.")
        client.force_login(moderator())
        delete = reverse("support:template-delete", args=[template.pk])

        assert client.get(reverse("support:templates")).status_code == 403
        assert client.post(delete).status_code == 403
        assert ReplyTemplate.objects.exists()


class TestChats:
    def test_the_list_shows_the_last_message_and_follows_the_live_signals(
        self, client: Client
    ) -> None:
        user = subscriber()
        chat = Conversation.objects.create(user=user, kind=Kind.CHAT)
        post(chat, user, "My key stopped working")
        client.force_login(admin())

        page = client.get(reverse("support:inbox-chats")).content.decode()

        assert "My key stopped working" in page
        assert 'hx-trigger="support:unread from:body, support:inbox from:body"' in page

    def test_a_chat_shows_the_client_card_with_open_requests(self, client: Client) -> None:
        user = subscriber(plan_name="Friends")
        chat = Conversation.objects.create(user=user, kind=Kind.CHAT)
        post(chat, user, "Hello?")
        request = request_of(user)
        client.force_login(admin())

        page = client.get(reverse("support:inbox-conversation", args=[chat.pk])).content.decode()

        assert "Friends" in page
        assert reverse("support:inbox-conversation", args=[request.pk]) in page
        assert reverse("dashboard:user", args=[user.pk]) in page

    def test_a_request_has_no_client_card(self, client: Client) -> None:
        conversation = request_of(subscriber())
        client.force_login(admin())

        page = client.get(reverse("support:inbox-conversation", args=[conversation.pk]))

        assert 'id="client-title"' not in page.content.decode()
