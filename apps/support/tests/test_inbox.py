from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.notifications.events import Event
from apps.notifications.models import Channel, ChannelKind, Notification, Route
from apps.plans.services.limits import effective_limits
from apps.portal.services.servers import Health, my_servers
from apps.servers.tests.factories import ServerFactory
from apps.support.forms import ChatFilterForm, RequestFilterForm
from apps.support.models import Conversation, Incident, Kind, TicketStatus, TicketType
from apps.support.services.inbox import Inbox, NotAllowedError
from apps.support.services.incidents import close_incident, incidents
from apps.support.services.thread import post
from apps.support.services.tickets import ClosedConversationError, MyRequests
from apps.support.tests.test_tickets import subscriber

pytestmark = pytest.mark.django_db


def admin() -> User:
    return UserFactory(role=Role.ADMIN)


def moderator() -> User:
    return UserFactory(role=Role.MODERATOR)


def served(user: User, member: User) -> User:
    user.manager = member
    user.save(update_fields=["manager"])
    return user


def request_of(user: User, text: str = "It is broken") -> Conversation:
    return MyRequests(user).open(TicketType.BUG, text)


class TestInbox:
    def test_an_admin_sees_everything_and_a_moderator_only_their_users(self) -> None:
        member = moderator()
        mine = request_of(served(subscriber(), member))
        taken = request_of(served(subscriber(), member))
        foreign = request_of(subscriber())
        Inbox(admin()).assign(taken, admin())
        Inbox(admin()).assign(foreign, member)

        assert Inbox(admin()).requests().count() == 3
        assert list(Inbox(member).requests()) == [mine]

    def test_only_an_admin_assigns(self) -> None:
        conversation = request_of(subscriber())

        with pytest.raises(NotAllowedError):
            Inbox(moderator()).assign(conversation, None)

    def test_unread_counts_only_the_users_messages(self) -> None:
        staff = admin()
        user = subscriber()
        conversation = request_of(user)
        post(conversation, user, "Any news?")
        Inbox(staff).note(conversation, "Looking")
        Inbox(staff).reply(conversation, "On it")

        assert Inbox(staff).requests().get().unread == 2  # type: ignore[attr-defined]
        assert Inbox(staff).unread() == 2

        Inbox(staff).mark_read(conversation)

        assert Inbox(staff).unread() == 0

    def test_a_reply_takes_a_new_request_into_work_and_a_note_stays_hidden(self) -> None:
        user = subscriber()
        conversation = request_of(user)
        inbox = Inbox(admin())

        inbox.reply(conversation, "Restart the app")
        inbox.note(conversation, "User is on an old version")

        conversation.refresh_from_db()
        assert conversation.status == TicketStatus.IN_PROGRESS
        seen = [message.text for message in MyRequests(user).messages(conversation)]
        assert seen == ["It is broken", "Restart the app"]

    def test_a_closed_request_takes_notes_but_no_replies(self) -> None:
        conversation = request_of(subscriber())
        inbox = Inbox(admin())
        inbox.set_status(conversation, TicketStatus.CLOSED)

        with pytest.raises(ClosedConversationError):
            inbox.reply(conversation, "Late answer")
        inbox.note(conversation, "Closed by phone")


class TestFilters:
    def test_requests_are_filtered_by_type_assignee_and_text(self) -> None:
        member = moderator()
        bug = request_of(subscriber(), "Keys do not import")
        MyRequests(subscriber()).open(TicketType.QUESTION, "How to share?")
        Inbox(admin()).assign(bug, member)
        requests = Inbox(admin()).requests()

        def found(**data: object) -> list[Conversation]:
            return list(RequestFilterForm(data).filter(requests))

        assert found(type=TicketType.BUG) == [bug]
        assert found(assignee=str(member.pk)) == [bug]
        assert len(found(assignee="none")) == 1
        assert found(search="import") == [bug]
        assert found(since=timezone.localdate() + timedelta(days=1)) == []

    def test_chats_with_unread_messages_come_first(self) -> None:
        quiet, waiting = subscriber(plan_name="Friends"), subscriber()
        old = Conversation.objects.create(user=waiting, kind=Kind.CHAT)
        post(old, waiting, "Hello?")
        new = Conversation.objects.create(user=quiet, kind=Kind.CHAT)
        post(new, admin(), "Welcome")
        chats = Inbox(admin()).chats()

        assert list(ChatFilterForm(None).filter(chats)) == [old, new]
        assert list(ChatFilterForm({"sort": "recent"}).filter(chats)) == [new, old]
        assert list(ChatFilterForm({"plan": quiet.limits.plan_id}).filter(chats)) == [new]

    def test_chats_waiting_for_an_answer_come_first_with_the_last_message(self) -> None:
        answered, waiting = subscriber(), subscriber()
        staff = admin()
        first = Conversation.objects.create(user=waiting, kind=Kind.CHAT)
        post(first, waiting, "Keys do not work")
        Inbox(staff).mark_read(first)
        second = Conversation.objects.create(user=answered, kind=Kind.CHAT)
        post(second, answered, "Hello?")
        Inbox(staff).reply(second, "Hi! How can we help?")
        Inbox(staff).note(second, "Asked twice before")

        chats = ChatFilterForm(None).filter(Inbox(staff).chats())

        assert list(chats.values_list("pk", "waiting", "unread", "last_text")) == [  # type: ignore[misc]
            (first.pk, True, 0, "Keys do not work"),
            (second.pk, False, 1, "Hi! How can we help?"),
        ]

    def test_requests_are_not_sorted_by_waiting(self) -> None:
        assert "waiting" not in dict(RequestFilterForm().fields["sort"].choices)  # type: ignore[attr-defined]


class TestIncidents:
    def test_ten_reports_of_one_server_are_one_incident(self) -> None:
        server = ServerFactory(name="Amsterdam")
        reporters = [subscriber(server) for _ in range(10)]

        for user in reporters:
            MyRequests(user).report_server(server)

        incident = incidents().get()
        assert incident.reports == 10  # type: ignore[attr-defined]

    def test_a_report_after_the_window_opens_a_new_incident(self) -> None:
        server = ServerFactory()
        MyRequests(subscriber(server)).report_server(server)
        Incident.objects.update(last_report_at=timezone.now() - timedelta(minutes=31))

        MyRequests(subscriber(server)).report_server(server)

        assert Incident.objects.count() == 2

    def test_the_staff_hear_once_an_incident_opens(self) -> None:
        channel = Channel.objects.create(name="ops", kind=ChannelKind.WEBHOOK, target="https://x")
        Route.objects.create(channel=channel, event=Event.INCIDENT_OPENED)
        server = ServerFactory(name="Amsterdam")

        MyRequests(subscriber(server)).report_server(server)
        MyRequests(subscriber(server)).report_server(server)

        notification = Notification.objects.get(event=Event.INCIDENT_OPENED)
        assert notification.data == {"server": "Amsterdam", "reports": 1}

    def test_the_status_page_shows_problems_while_the_incident_is_open(self) -> None:
        server = ServerFactory()
        user = subscriber(server)
        MyRequests(user).report_server(server)

        [state] = my_servers(effective_limits(user))
        assert state.health == Health.PROBLEMS

        close_incident(Incident.objects.get(), admin())

        [state] = my_servers(effective_limits(user))
        assert state.health != Health.PROBLEMS

    def test_closing_an_incident_closes_its_requests_and_tells_the_users(self) -> None:
        server = ServerFactory(name="Amsterdam")
        users = [subscriber(server) for _ in range(3)]
        for user in users:
            MyRequests(user).report_server(server)

        close_incident(Incident.objects.get(), admin(), "Fixed, sorry for that")

        requests = Conversation.objects.filter(type=TicketType.SERVER_DOWN)
        assert {request.status for request in requests} == {TicketStatus.CLOSED}
        assert all(r.messages.get().text == "Fixed, sorry for that" for r in requests)
        told = Notification.objects.filter(event=Event.INCIDENT_RESOLVED)
        assert {notification.user for notification in told} == set(users)
        assert Incident.objects.get().closed_at is not None
