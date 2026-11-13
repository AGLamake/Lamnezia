import ast
from pathlib import Path
from typing import Any

import pytest

from apps.accounts.models import LinkedAccount, Messenger, Status, User
from apps.accounts.services.linking import AccountLinks
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.bots.services.core import BotCore, language_of
from apps.bots.services.replies import Command, Incoming, Placement, split_text
from apps.bots.services.session import SiteLinks, Throttle
from apps.bots.tests.conftest import SITE, link
from apps.bots.tests.fake_adapter import FakeAdapter
from apps.portal.tests.test_portal import subscriber
from apps.support.models import Conversation

pytestmark = pytest.mark.django_db

BOTS = Path(__file__).resolve().parents[1]


def labels(reply: object) -> list[str]:
    return [button.label for button in reply.buttons]  # type: ignore[attr-defined]


def test_the_core_does_not_import_a_messenger_library() -> None:
    """Only the adapters and their tests know a messenger."""
    imported: set[str] = set()
    outside = (BOTS / "adapters", BOTS / "tests")
    for path in BOTS.rglob("*.py"):
        if path.parent in outside:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

    assert not imported & {"aiogram", "discord", "telegram"}


class TestNotLinked:
    @pytest.mark.parametrize("text", ["/start", "/menu", "/keys", "/status", "/servers"])
    def test_any_command_only_tells_how_to_link(self, bot: FakeAdapter, text: str) -> None:
        subscriber()

        reply = bot.say(text)

        assert "not linked" in reply.text
        assert [button.url for button in reply.buttons] == [f"{SITE}/accounts/messengers/"]
        assert not reply.code and not reply.files

    def test_a_typed_message_reaches_nobody(self, bot: FakeAdapter) -> None:
        reply = bot.say("my VPN does not work")

        assert "not linked" in reply.text
        assert not Conversation.objects.exists()
        assert not AuditEvent.objects.exists()

    @pytest.mark.parametrize("command", ["/start", "/link"])
    def test_a_code_from_the_profile_links_the_account(
        self, bot: FakeAdapter, command: str
    ) -> None:
        user = UserFactory(username="anna")
        code = AccountLinks().issue_code(user, Messenger.TELEGRAM)

        reply = bot.say(f"{command} {code}")

        assert "Linked to anna" in reply.text
        assert AccountLinks.user_of(Messenger.TELEGRAM, "1001") == user
        event = AuditEvent.objects.get(action="accounts.link")
        assert event.data == {"messenger": "telegram", "via": "bot"}

    def test_a_wrong_code_links_nothing(self, bot: FakeAdapter) -> None:
        reply = bot.say("/start wrong-code")

        assert [button.url for button in reply.buttons] == [f"{SITE}/accounts/messengers/"]
        assert not LinkedAccount.objects.exists()


class TestLinked:
    def test_menu_offers_every_topic(self, bot: FakeAdapter, user: User) -> None:
        assert labels(bot.say("/start")) == [
            "Status",
            "My keys",
            "New key",
            "Servers",
            "A server does not work",
            "Support",
            "Guides",
        ]

    def test_help_lists_the_commands(self, bot: FakeAdapter, user: User) -> None:
        assert "/newkey" in bot.say("/help").text

    def test_an_unknown_command_points_to_the_menu(self, bot: FakeAdapter, user: User) -> None:
        reply = bot.say("/dance")

        assert "do not know" in reply.text
        assert labels(reply) == ["Menu"]

    def test_status_shows_traffic_and_access(self, bot: FakeAdapter) -> None:
        link(subscriber(traffic_limit=10 * 1024**3))

        lines = bot.say("/status").text.splitlines()

        assert lines[0].startswith("• Traffic: 0\xa0bytes of 10.0\xa0GB. Resets on ")
        assert lines[-1] == "• Access has no end date."

    def test_status_without_a_plan(self, bot: FakeAdapter) -> None:
        link(UserFactory())

        assert "no plan yet" in bot.say("/status").text

    def test_guides_open_the_pages_of_each_device(self, bot: FakeAdapter, user: User) -> None:
        urls = [button.url for button in bot.say("/guides").buttons]

        assert f"{SITE}/guides/android/" in urls and urls[-2] == f"{SITE}/guides/faq/"

    def test_a_command_is_journaled_without_its_arguments(
        self, bot: FakeAdapter, user: User
    ) -> None:
        bot.press("card 12345")

        event = AuditEvent.objects.get(action="bots.command")
        assert (event.actor, event.data) == (user, {"messenger": "telegram", "command": "card"})

    def test_the_bot_answers_in_the_users_language(self, bot: FakeAdapter, user: User) -> None:
        # Django's own catalog is always compiled: its words show the language in use.
        assert "байт" in bot.say("/status", locale="ru-RU").text
        assert "bytes" in bot.say("/status", locale="en").text

    def test_unlink_asks_first(self, bot: FakeAdapter, user: User) -> None:
        question = bot.say("/unlink")
        assert LinkedAccount.objects.filter(user=user).exists()

        bot.press_button(question, "Unlink")

        assert not LinkedAccount.objects.filter(user=user).exists()
        assert AuditEvent.objects.filter(action="accounts.unlink").exists()
        assert "not linked" in bot.say("/menu").text


class TestNotApproved:
    @pytest.fixture
    def pending(self) -> User:
        user = link(subscriber(max_keys=2))
        user.status = Status.PENDING
        user.save()
        return user

    def test_menu_has_only_status_and_guides(self, bot: FakeAdapter, pending: User) -> None:
        assert labels(bot.say("/menu")) == ["Status", "Guides"]

    def test_status_says_the_registration_waits(self, bot: FakeAdapter, pending: User) -> None:
        assert "waits for approval" in bot.say("/status").text

    @pytest.mark.parametrize("text", ["/keys", "/newkey", "/servers", "/down", "hello"])
    def test_everything_else_is_closed(self, bot: FakeAdapter, pending: User, text: str) -> None:
        assert "until your account is approved" in bot.say(text).text
        assert not Conversation.objects.exists()

    def test_a_blocked_user_is_told_so(self, bot: FakeAdapter, pending: User) -> None:
        pending.status = Status.BLOCKED
        pending.save()

        assert "blocked" in bot.say("/status").text


def test_commands_over_the_rate_limit_are_refused(user: User) -> None:
    bot = FakeAdapter(core=BotCore(links=SiteLinks(SITE), throttle=Throttle(limit=2)))

    replies = [bot.say("/help").text for _ in range(3)]

    assert "Too many commands" not in replies[1]
    assert "Too many commands" in replies[2]
    assert "Too many commands" not in FakeAdapter("2002", bot.core).say("/help").text


@pytest.mark.parametrize(
    ("locale", "language"), [("ru", "ru"), ("en-US", "en"), ("pt_BR", "ru"), ("", "ru")]
)
def test_language_of_the_messenger(locale: str, language: str, settings: Any) -> None:
    settings.LANGUAGE_CODE = "ru"
    assert language_of(locale) == language


@pytest.mark.parametrize(
    ("incoming", "command"),
    [
        (Incoming(Messenger.TELEGRAM, "1", text="/Start@lamnezia_bot abc"), ("start", ("abc",))),
        (Incoming(Messenger.TELEGRAM, "1", text="hello there"), ("", ())),
        (
            Incoming(Messenger.TELEGRAM, "1", action="newkey awg:1 ios"),
            ("newkey", ("awg:1", "ios")),
        ),
    ],
)
def test_commands_are_parsed(incoming: Incoming, command: tuple[str, tuple[str, ...]]) -> None:
    parsed = Command.of(incoming)

    assert (parsed.name, parsed.args) == command


def test_a_press_over_the_rate_limit_gets_a_popup_and_keeps_the_panel(user: User) -> None:
    bot = FakeAdapter(core=BotCore(links=SiteLinks(SITE), throttle=Throttle(limit=1)))
    bot.say("/menu")

    reply = bot.press("keys")

    assert reply.placement == Placement.POPUP
    assert "Too many commands" in reply.text
    assert bot.kinds == ["user", "panel", "popup"]
    assert bot.panels[0].edits == 0


@pytest.mark.parametrize(
    ("site", "public"),
    [
        ("https://vpn.example", True),
        ("http://203.0.113.5:8000", False),  # documentation range, not routed
        ("http://8.8.8.8", True),
        ("http://localhost:8000", False),
        ("http://127.0.0.1:8000", False),
        ("http://192.168.1.10", False),
        ("http://panel", False),
        ("http://panel.local", False),
        ("http://web.localhost", False),
    ],
)
def test_a_site_is_public_on_an_internet_domain_only(site: str, public: bool) -> None:
    assert SiteLinks(site).public is public


def test_without_a_public_domain_links_go_in_the_text(user: User) -> None:
    bot = FakeAdapter(core=BotCore(links=SiteLinks("http://localhost:8000"), throttle=Throttle(0)))

    reply = bot.say("/guides")

    assert all(not button.url for button in reply.buttons)
    assert "Questions and answers: http://localhost:8000/" in reply.text
    assert labels(reply)[-1] == "Menu"


def test_on_a_public_domain_links_stay_buttons(bot: FakeAdapter, user: User) -> None:
    reply = bot.say("/guides")

    assert any(button.url.startswith(SITE) for button in reply.buttons)
    assert SITE not in reply.text


@pytest.mark.parametrize(
    ("text", "pieces"),
    [
        ("", [""]),
        ("abc", ["abc"]),
        ("abcdefgh", ["abcd", "efgh"]),
        ("ab\ncdef\ngh", ["ab", "cdef", "gh"]),
        ("abcdef\ngh", ["abcde", "f\ngh"]),
    ],
)
def test_long_text_is_split_at_line_breaks(text: str, pieces: list[str]) -> None:
    assert split_text(text, 5 if "\n" in text else 4) == pieces


def test_the_split_counts_by_the_given_length() -> None:
    def double(text: str) -> int:
        return 2 * len(text)

    assert split_text("abcdef", 4, double) == ["ab", "cd", "ef"]
