import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.servers.tests.factories import ServerFactory
from apps.servers.tests.test_monitor import FakeServers
from apps.support.models import Conversation, TicketStatus, TicketType
from apps.support.services.check import RequestServerCheck
from apps.support.services.inbox import Inbox
from apps.support.services.thread import post
from apps.support.services.tickets import MyRequests
from apps.support.tests.test_inbox import admin
from apps.support.tests.test_tickets import png, subscriber

pytestmark = pytest.mark.django_db


def test_a_problem_report_is_sent_with_a_screenshot(client: Client) -> None:
    user = subscriber()
    client.force_login(user)

    response = client.post(
        reverse("support:new"),
        {"type": TicketType.BUG, "text": "Nothing opens", "image": png()},
        HTTP_USER_AGENT="Firefox",
    )

    conversation = Conversation.objects.get(user=user)
    assert response["Location"] == reverse("support:conversation", args=[conversation.pk])
    assert conversation.context["user_agent"] == "Firefox"
    attachment = conversation.messages.get().attachments.get()
    image = client.get(reverse("support:attachment", args=[attachment.pk]))
    assert image["Content-Type"] == "image/png"


def test_a_file_that_is_not_an_image_is_not_accepted(client: Client) -> None:
    client.force_login(subscriber())

    response = client.post(
        reverse("support:new"),
        {"type": TicketType.BUG, "text": "x", "image": png("shot.png", b"<html>")},
    )

    assert response.status_code == 200
    assert not Conversation.objects.exists()


def test_server_down_is_one_click_from_the_status_page(client: Client) -> None:
    server = ServerFactory()
    user = subscriber(server)
    client.force_login(user)
    status = client.get(reverse("portal:servers"))
    assert reverse("support:server-down").encode() in status.content

    response = client.post(reverse("support:server-down"), {"server": server.pk})

    conversation = Conversation.objects.get(user=user, type=TicketType.SERVER_DOWN)
    assert response["Location"] == reverse("support:conversation", args=[conversation.pk])
    page = client.get(response["Location"])
    assert page.status_code == 200
    assert page.context["checking"]


def test_a_server_outside_the_plan_cannot_be_reported(client: Client) -> None:
    allowed, other = ServerFactory(), ServerFactory()
    client.force_login(subscriber(allowed))

    response = client.post(reverse("support:server-down"), {"server": other.pk})

    assert response.status_code == 404


def test_a_user_sees_only_their_own_requests(client: Client) -> None:
    mine = MyRequests(subscriber()).open(TicketType.QUESTION, "Mine")
    theirs = MyRequests(subscriber()).open(TicketType.QUESTION, "Theirs")
    client.force_login(mine.user)

    listing = client.get(reverse("support:index"))
    other = client.get(reverse("support:conversation", args=[theirs.pk]))
    check = client.get(reverse("support:check", args=[theirs.pk]))

    assert list(listing.context["conversations"]) == [mine]
    assert other.status_code == 404
    assert check.status_code == 404


def test_internal_notes_and_their_images_stay_hidden(client: Client) -> None:
    user, admin = subscriber(), UserFactory(role="admin")
    conversation = MyRequests(user).open(TicketType.QUESTION, "Hello")
    note = post(conversation, admin, "Internal: user is on an old app", png(), internal=True)
    other = MyRequests(subscriber()).open(TicketType.BUG, "x", png())
    client.force_login(user)

    page = client.get(reverse("support:conversation", args=[conversation.pk]))
    note_image = client.get(reverse("support:attachment", args=[note.attachments.get().pk]))
    foreign = client.get(
        reverse("support:attachment", args=[other.messages.get().attachments.get().pk])
    )

    assert b"Internal" not in page.content
    assert note_image.status_code == 403
    assert foreign.status_code == 403
    client.force_login(admin)
    assert (
        client.get(reverse("support:attachment", args=[note.attachments.get().pk])).status_code
        == 200
    )


def test_a_moderator_sees_images_of_their_zone_only(client: Client) -> None:
    moderator = UserFactory(role="moderator")
    mine = MyRequests(UserFactory(manager=moderator)).open(TicketType.BUG, "x", png())
    foreign = MyRequests(subscriber()).open(TicketType.BUG, "y", png())
    # Assigning a conversation does not take it into the moderator's zone.
    Conversation.objects.filter(pk=foreign.pk).update(assignee=moderator)
    client.force_login(moderator)

    def status(conversation: Conversation) -> int:
        attachment = conversation.messages.get().attachments.get()
        return client.get(reverse("support:attachment", args=[attachment.pk])).status_code

    assert (status(mine), status(foreign)) == (200, 403)


def test_a_reply_by_htmx_returns_the_thread(client: Client) -> None:
    conversation = MyRequests(subscriber()).open(TicketType.QUESTION, "Hello")
    client.force_login(conversation.user)

    response = client.post(
        reverse("support:conversation", args=[conversation.pk]),
        {"text": "More details"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert b"More details" in response.content
    assert conversation.messages.count() == 2


def test_pages_need_a_sign_in(client: Client) -> None:
    response = client.get(reverse("support:index"))

    assert response.status_code == 302


def test_the_check_result_shows_in_the_thread(client: Client) -> None:
    server = ServerFactory()
    conversation = MyRequests(subscriber(server)).report_server(server)
    RequestServerCheck(FakeServers().connect).run(conversation, server)
    client.force_login(conversation.user)

    response = client.get(reverse("support:check", args=[conversation.pk]))

    assert b"The server answers." in response.content
    assert server.host.encode() not in response.content


def test_closing_a_request_hides_the_form_on_an_open_page(client: Client) -> None:
    conversation = MyRequests(subscriber()).open(TicketType.QUESTION, "Hello")
    client.force_login(conversation.user)
    url = reverse("support:messages", args=[conversation.pk])

    open_page = client.get(url)
    Inbox(admin()).set_status(conversation, TicketStatus.CLOSED)
    closed_page = client.get(url)

    assert b'id="reply"' not in open_page.content
    assert b'id="reply" class="help-text" hx-swap-oob="true"' in closed_page.content
