from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.bots.tests.conftest import link
from apps.bots.tests.fake_adapter import FakeAdapter
from apps.protocols.models import ProtocolInstance
from apps.servers.models import Server, ServerSample
from apps.servers.tests.factories import ServerFactory
from apps.support.models import Conversation, Incident, Kind, TicketStatus, TicketType
from apps.support.services.inbox import Inbox
from apps.support.services.tickets import MyRequests
from apps.support.tests.test_tickets import instance_on, subscriber
from apps.traffic.models import TrafficHourly

pytestmark = pytest.mark.django_db

NOT_AVAILABLE = "This is not available to you."
GB = 1024**3


def staff(role: str = Role.ADMIN) -> User:
    return link(UserFactory(role=role))


def report(server: Server) -> Conversation:
    return MyRequests(subscriber(server)).report_server(server)


def labels(bot: FakeAdapter) -> list[str]:
    return [button.label for button in bot.replies[-1].buttons]


class TestAccess:
    @pytest.mark.parametrize(
        "action", ["admin", "overview", "today", "incidents", "incident 1", "take 1", "close 1"]
    )
    def test_users_without_a_staff_role_cannot_run_the_commands(
        self, bot: FakeAdapter, user: User, action: str
    ) -> None:
        incident = Incident.objects.create(server=ServerFactory(), pk=1)

        assert bot.press(action).text == NOT_AVAILABLE
        assert bot.say(f"/{action}").text == NOT_AVAILABLE
        assert Incident.objects.get(pk=incident.pk).closed_at is None

    def test_a_user_replying_to_a_bot_message_just_talks_to_the_bot(
        self, bot: FakeAdapter, user: User
    ) -> None:
        request = MyRequests(subscriber()).open(TicketType.BUG, "It is broken")

        bot.reply_to(f"New request #{request.pk} from boris: It is broken", "Hello")

        assert not request.messages.filter(text="Hello").exists()

    def test_only_the_staff_see_the_administration_button(self, bot: FakeAdapter) -> None:
        member = staff(Role.MODERATOR)
        bot.press("menu")
        assert "Administration" in labels(bot)

        member.role = Role.USER
        member.save()
        bot.press("menu")
        assert "Administration" not in labels(bot)


class TestOverview:
    def test_servers_with_their_state_load_and_incidents(self, bot: FakeAdapter) -> None:
        staff()
        online = ServerFactory(name="Amsterdam")
        online.record_sample(
            ServerSample.objects.create(
                server=online,
                at=timezone.now(),
                cpu_percent=12.4,
                memory_total=100,
                memory_available=60,
            )
        )
        silent = ServerFactory(name="Berlin", maintenance=True)
        report(silent)

        text = bot.press("overview").text

        assert "Amsterdam: online · CPU 12% · memory 40%" in text
        assert "Berlin: no readings yet · under maintenance · open incident" in text

    def test_traffic_since_midnight_by_server(self, bot: FakeAdapter) -> None:
        staff()
        server = ServerFactory(name="Amsterdam")
        instance_on(server)
        (source,) = ProtocolInstance.sources_of(server)
        midnight = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        for hour, rx in ((midnight, 2 * GB), (midnight - timedelta(hours=1), 5 * GB)):
            TrafficHourly.objects.create(source=source, public_key="k", hour=hour, rx_bytes=rx)

        text = bot.press("today").text.replace("\xa0", " ")

        assert "All servers: 2.0 GB" in text
        assert "Amsterdam: 2.0 GB" in text

    def test_a_moderator_hears_only_of_their_servers(self, bot: FakeAdapter) -> None:
        member = staff(Role.MODERATOR)
        member.moderated_servers.add(ServerFactory(name="Amsterdam"))
        report(ServerFactory(name="Berlin"))

        overview = bot.press("overview").text
        incidents = bot.press("incidents").text

        assert "Amsterdam" in overview
        assert "Berlin" not in overview
        assert incidents == "No open incidents."


class TestIncidents:
    def test_open_incidents_are_listed_with_a_button_each(self, bot: FakeAdapter) -> None:
        staff()
        server = ServerFactory(name="Amsterdam")
        report(server)
        report(server)
        incident = Incident.objects.get()

        reply = bot.press("incidents")

        assert "Amsterdam since" in reply.text
        assert "reports: 2" in reply.text
        assert reply.buttons[0].action == f"incident {incident.pk}"

    def test_a_moderator_takes_an_incident_but_cannot_close_it(self, bot: FakeAdapter) -> None:
        member = staff(Role.MODERATOR)
        server = ServerFactory()
        member.moderated_servers.add(server)
        request = report(server)
        User.objects.filter(pk=request.user_id).update(manager=member)
        incident = Incident.objects.get()

        card = bot.press(f"incident {incident.pk}")
        assert "Nobody has taken it." in card.text
        assert "Close" not in labels(bot)

        taken = bot.press_button(card, "Take")

        request.refresh_from_db()
        assert request.assignee == member
        assert request.status == TicketStatus.IN_PROGRESS
        assert f"Taken by: {member.username}" in taken.text
        assert bot.press(f"close {incident.pk} yes").text == NOT_AVAILABLE
        assert Incident.objects.get().closed_at is None

    def test_a_moderator_takes_only_the_requests_of_their_users(self, bot: FakeAdapter) -> None:
        member = staff(Role.MODERATOR)
        server = ServerFactory()
        member.moderated_servers.add(server)
        own, foreign = report(server), report(server)
        User.objects.filter(pk=own.user_id).update(manager=member)
        incident = Incident.objects.get()

        taken = bot.press(f"take {incident.pk}")

        own.refresh_from_db()
        foreign.refresh_from_db()
        assert own.assignee == member
        assert foreign.assignee is None
        assert foreign.status == TicketStatus.NEW
        assert "its requests are yours now" in taken.text

    def test_a_moderator_without_users_in_the_incident_takes_nothing(
        self, bot: FakeAdapter
    ) -> None:
        member = staff(Role.MODERATOR)
        server = ServerFactory()
        member.moderated_servers.add(server)
        request = report(server)
        incident = Incident.objects.get()

        taken = bot.press(f"take {incident.pk}")

        request.refresh_from_db()
        assert request.assignee is None
        assert "None of its requests are from your users." in taken.text

    def test_an_admin_closes_an_incident_after_confirming(self, bot: FakeAdapter) -> None:
        staff()
        request = report(ServerFactory(name="Amsterdam"))
        incident = Incident.objects.get()

        question = bot.press(f"close {incident.pk}")
        assert Incident.objects.get().closed_at is None

        closed = bot.press_button(question, "Close")

        assert closed.text == "The incident is closed; the users are told."
        assert Incident.objects.get().closed_at is not None
        request.refresh_from_db()
        assert request.status == TicketStatus.CLOSED


class TestReply:
    def test_an_admin_answers_a_request_by_replying_to_its_notification(
        self, bot: FakeAdapter
    ) -> None:
        staff()
        request = MyRequests(subscriber()).open(TicketType.BUG, "It is broken")

        reply = bot.reply_to(f"New request #{request.pk} from boris: It is broken", "On it")

        assert reply.text == f"Sent to {request.user.username}."
        assert request.messages.filter(text="On it", internal=False).exists()

    def test_a_reply_to_a_chat_message_goes_to_the_chat(self, bot: FakeAdapter) -> None:
        staff()
        boris = subscriber()
        chat = Conversation.objects.create(user=boris, kind=Kind.CHAT)

        bot.reply_to(f"Message from {boris.username}, chat #{chat.pk}", "Hi")

        assert chat.messages.filter(text="Hi").exists()

    def test_a_moderator_answers_only_their_users(self, bot: FakeAdapter) -> None:
        member = staff(Role.MODERATOR)
        theirs = MyRequests(UserFactory(manager=member)).open(TicketType.BUG, "It is broken")
        other = MyRequests(subscriber()).open(TicketType.BUG, "Also broken")
        Inbox(UserFactory(role=Role.ADMIN)).assign(other, member)

        refused = bot.reply_to(f"New request #{other.pk} from boris: Also broken", "On it")
        sent = bot.reply_to(f"New request #{theirs.pk} from boris: It is broken", "On it")

        assert refused.text == NOT_AVAILABLE
        assert not other.messages.filter(text="On it").exists()
        assert sent.text.startswith("Sent to")
