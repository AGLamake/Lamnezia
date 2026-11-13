import pytest

from apps.accounts.models import User
from apps.bots.tests.conftest import link
from apps.bots.tests.fake_adapter import FakeAdapter
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.support.models import Conversation, TicketType
from apps.support.tests.test_tickets import subscriber

pytestmark = pytest.mark.django_db


@pytest.fixture
def server() -> Server:
    return ServerFactory(name="Amsterdam", location="NL")


@pytest.fixture
def user(server: Server) -> User:
    return link(subscriber(server))


def test_servers_shows_the_health_of_the_plans_servers(
    bot: FakeAdapter, user: User, server: Server
) -> None:
    ServerFactory(name="Other")

    reply = bot.say("/servers")

    assert reply.text == "• Amsterdam (NL): no data yet"
    assert reply.buttons[0].action == "down"


def test_report_a_server_with_a_comment(bot: FakeAdapter, user: User, server: Server) -> None:
    choose = bot.say("/down")
    asking = bot.press_button(choose, "Amsterdam")

    thanks = bot.say("No handshake since the morning")

    assert "Thank you" in thanks.text and "send" in asking.buttons[0].action
    conversation = Conversation.objects.get(user=user)
    assert (conversation.type, conversation.server) == (TicketType.SERVER_DOWN, server)
    assert conversation.messages.get().text == "No handshake since the morning"
    assert conversation.context["user_agent"] == "Telegram bot"


def test_report_a_server_without_a_comment(bot: FakeAdapter, user: User, server: Server) -> None:
    asking = bot.press(f"down {server.pk}")

    bot.press_button(asking, "Send without a comment")

    assert Conversation.objects.get(user=user).type == TicketType.SERVER_DOWN
    # The dialog has ended: the next message goes to the chat, not to the report.
    bot.say("thanks")
    assert Conversation.objects.filter(user=user).count() == 2


def test_a_command_cancels_the_waiting_comment(
    bot: FakeAdapter, user: User, server: Server
) -> None:
    bot.press(f"down {server.pk}")
    bot.say("/menu")

    bot.say("hello")

    (chat,) = Conversation.objects.filter(user=user)
    assert chat.type == "" and chat.messages.get().text == "hello"


def test_a_server_outside_the_plan_cannot_be_reported(bot: FakeAdapter, user: User) -> None:
    other = ServerFactory()

    assert "not available" in bot.press(f"down {other.pk}").text
    assert not Conversation.objects.exists()


def test_a_typed_message_goes_to_the_support_chat(bot: FakeAdapter, user: User) -> None:
    assert "administrators" in bot.say("/support").text

    reply = bot.say("How do I set it up on a TV?")

    assert reply.text == "Sent to the administrators."
    (chat,) = Conversation.objects.filter(user=user)
    assert chat.messages.get().text == "How do I set it up on a TV?"
