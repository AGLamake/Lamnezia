"""The Discord adapter: interactions and direct messages in, answers out, kept by fakes."""

from dataclasses import dataclass, field
from typing import Any, cast

import discord
import pytest
from discord import app_commands
from django.utils import translation
from django.utils.translation import gettext

from apps.accounts.models import LinkedAccount, Messenger
from apps.bots.adapters.discord import (
    ACTION_PREFIX,
    BUTTON_LIMIT,
    TEXT_LIMIT,
    ActionButton,
    DiscordAdapter,
    MenuTranslator,
    messages_of,
    slash_commands,
)
from apps.bots.services.core import BotCore
from apps.bots.services.menu import MENU
from apps.bots.services.replies import Attachment, Button, Incoming, Placement, Reply
from apps.bots.services.session import SiteLinks
from apps.bots.tests.conftest import SITE
from apps.keys.models import AccessKey
from apps.keys.tests.conftest import SOURCE
from apps.portal.tests.test_portal import subscriber

ANNA_ID = 501


@dataclass
class Sender:
    """The user; `dm` keeps what the bot wrote to their direct messages."""

    id: int = ANNA_ID
    name: str = "anna"
    bot: bool = False
    dm: list[dict[str, Any]] = field(default_factory=list)
    dm_closed: bool = False

    async def send(self, **message: Any) -> None:
        if self.dm_closed:
            raise discord.Forbidden(cast(Any, Refused()), "Cannot send messages to this user")
        self.dm.append(message)


@dataclass
class Response:
    deferred: list[bool] = field(default_factory=list)

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.deferred.append(ephemeral)


@dataclass
class Followup:
    sent: list[dict[str, Any]] = field(default_factory=list)

    async def send(self, **message: Any) -> None:
        self.sent.append(message)


@dataclass
class FakeInteraction:
    """What the adapter reads of an interaction: who, where, in what language; and the answers.

    `edits` are the redraws of the message whose button was pressed.
    """

    guild: object | None = None
    user: Sender = field(default_factory=Sender)
    locale: discord.Locale = discord.Locale.russian
    response: Response = field(default_factory=Response)
    followup: Followup = field(default_factory=Followup)
    edits: list[dict[str, Any]] = field(default_factory=list)
    refuse_edits: bool = False

    async def edit_original_response(self, **message: Any) -> None:
        if self.refuse_edits:
            raise discord.NotFound(cast(Any, FakeHttpResponse()), "Unknown Message")
        self.edits.append(message)

    @property
    def sent(self) -> list[dict[str, Any]]:
        return self.followup.sent


class FakeHttpResponse:
    status = 404
    reason = "Not Found"


class Refused:
    status = 403
    reason = "Forbidden"


@dataclass
class FakeChannel:
    sent: list[dict[str, Any]] = field(default_factory=list)

    async def send(self, **message: Any) -> None:
        self.sent.append(message)


class FakeDm(FakeChannel, discord.DMChannel):  # type: ignore[misc]
    """Passes the adapter's DM check without a gateway connection."""

    def __init__(self) -> None:
        FakeChannel.__init__(self)


@dataclass
class FakeMessage:
    content: str
    channel: FakeChannel
    author: Sender = field(default_factory=Sender)
    reference: Any = None
    attachments: list[Any] = field(default_factory=list)


class Replied(discord.Message):
    """A message the user replies to; passes the adapter's check without a gateway."""

    def __init__(self, content: str, author: Sender) -> None:
        self.content = content
        self.author = cast(Any, author)


class StubCore(BotCore):
    """Gives one reply to anything and keeps what the adapter passed."""

    def __init__(self, reply: Reply) -> None:
        super().__init__()
        self.reply = reply
        self.received: list[Incoming] = []

    def handle(self, incoming: Incoming) -> Reply:
        self.received.append(incoming)
        return self.reply


def _interaction(*, on_server: bool = False) -> Any:
    return FakeInteraction(guild=object() if on_server else None)


@pytest.mark.asyncio
async def test_a_command_goes_to_the_core_with_the_sender() -> None:
    core = StubCore(Reply("Linked."))
    interaction = _interaction()

    await DiscordAdapter(core).on_command(interaction, "link abc")

    assert core.received == [
        Incoming(Messenger.DISCORD, "501", "anna", text="/link abc", locale="ru")
    ]
    assert interaction.response.deferred == [False]
    assert interaction.sent == [{"content": "Linked.", "ephemeral": False}]


@pytest.mark.asyncio
async def test_a_command_on_a_server_opens_the_panel_in_direct_messages() -> None:
    reply = Reply("Your key", code="vpn://abc", private=True)
    interaction = _interaction(on_server=True)

    await DiscordAdapter(StubCore(reply)).on_command(interaction, "menu")

    assert interaction.response.deferred == [True]
    assert interaction.user.dm == [{"content": "```\nvpn://abc\n```"}, {"content": "Your key"}]
    with translation.override("ru"):
        note = gettext("I wrote to you in direct messages.")
    # Only the user who asked sees the note; nothing of the panel is left on the server.
    assert interaction.sent == [{"content": note, "ephemeral": True}]


@pytest.mark.asyncio
async def test_closed_direct_messages_get_a_hint_instead_of_an_error() -> None:
    interaction = _interaction(on_server=True)
    interaction.user.dm_closed = True

    await DiscordAdapter(StubCore(Reply("Menu"))).on_command(interaction, "menu")

    assert interaction.user.dm == []
    (hint,) = interaction.sent
    assert hint["ephemeral"] is True
    with translation.override("ru"):
        assert hint["content"] == gettext(
            "I can't write to you: your direct messages are closed. Open the server menu, go to "
            "Privacy Settings, allow direct messages from server members and repeat the command."
        )


@pytest.mark.asyncio
async def test_a_press_runs_the_action_of_the_button() -> None:
    core = StubCore(Reply("Card"))

    await DiscordAdapter(core).on_button(_interaction(), "card 12")

    assert core.received[0].action == "card 12"


@pytest.mark.asyncio
async def test_a_press_redraws_the_panel_in_place() -> None:
    reply = Reply("phone · active", (Button("Back", "keys"),), title="phone*")
    interaction = _interaction(on_server=True)

    await DiscordAdapter(StubCore(reply.placed(Placement.REPLACE))).on_button(
        interaction, "card 12"
    )

    assert interaction.response.deferred == [False]  # an update of the message, not a new one
    assert interaction.sent == []
    (edit,) = interaction.edits
    assert edit["content"] == "**phone\\***\n\nphone · active"
    assert edit["attachments"] == []
    assert edit["view"].children[0].custom_id == ACTION_PREFIX + "keys"


@pytest.mark.asyncio
async def test_a_redrawn_panel_without_buttons_loses_the_old_ones() -> None:
    interaction = _interaction()

    await DiscordAdapter(StubCore(Reply("Done", placement=Placement.REPLACE))).on_button(
        interaction, "unlink yes"
    )

    assert interaction.edits == [{"content": "Done", "view": None, "attachments": []}]


@pytest.mark.asyncio
async def test_a_panel_that_cannot_be_edited_is_sent_anew() -> None:
    interaction = _interaction()
    interaction.refuse_edits = True

    await DiscordAdapter(StubCore(Reply("Menu", placement=Placement.REPLACE))).on_button(
        interaction, "menu"
    )

    assert interaction.sent == [{"content": "Menu", "ephemeral": False}]


@pytest.mark.asyncio
async def test_a_panel_longer_than_a_message_is_sent_anew() -> None:
    interaction = _interaction()
    reply = Reply("a" * (TEXT_LIMIT + 1), placement=Placement.REPLACE)

    await DiscordAdapter(StubCore(reply)).on_button(interaction, "requests")

    assert interaction.edits == []
    assert len(interaction.sent) == 2


@pytest.mark.asyncio
async def test_a_direct_message_goes_to_the_core() -> None:
    core = StubCore(Reply("Thanks"))
    dm = FakeDm()

    await DiscordAdapter(core).on_message(cast(Any, FakeMessage("my question", dm)))

    assert core.received[0].text == "my question"
    assert dm.sent == [{"content": "Thanks"}]


@pytest.mark.asyncio
async def test_a_reply_to_a_message_of_the_bot_brings_its_text() -> None:
    core = StubCore(Reply("Sent."))
    dm = FakeDm()
    original = discord.MessageReference(message_id=3, channel_id=1)
    original.resolved = Replied("New request #12 from boris", Sender(bot=True))
    original_of_anna = discord.MessageReference(message_id=4, channel_id=1)
    original_of_anna.resolved = Replied("#13", Sender())
    adapter = DiscordAdapter(core)

    await adapter.on_message(cast(Any, FakeMessage("On it", dm, reference=original)))
    await adapter.on_message(cast(Any, FakeMessage("Me too", dm, reference=original_of_anna)))

    assert [incoming.replied for incoming in core.received] == ["New request #12 from boris", ""]


@pytest.mark.asyncio
async def test_messages_on_servers_and_from_bots_are_not_read() -> None:
    core = StubCore(Reply("Your key", private=True))
    adapter = DiscordAdapter(core)
    channel = FakeChannel()

    await adapter.on_message(cast(Any, FakeMessage("/keys", channel)))
    await adapter.on_message(cast(Any, FakeMessage("hi", FakeDm(), Sender(bot=True))))

    assert core.received == []
    assert channel.sent == []


@pytest.mark.asyncio
async def test_a_key_comes_as_a_code_block_with_the_files_and_buttons() -> None:
    qr = Attachment("key.png", b"png", "image/png")
    conf = Attachment("key.conf", b"[Interface]", "text/plain")
    reply = Reply(
        "Your key", (Button("My keys", "keys"),), code="vpn://abc", pictures=(qr,), files=(conf,)
    )

    code, files, panel = messages_of(reply)

    assert code == {"content": "```\nvpn://abc\n```"}
    assert [file.filename for file in files["files"]] == ["key.png", "key.conf"]
    assert "content" not in files and "view" not in files
    assert panel["content"] == "Your key"
    (button,) = panel["view"].children
    assert (button.item.label, button.custom_id) == ("My keys", ACTION_PREFIX + "keys")


@pytest.mark.asyncio
async def test_long_text_goes_on_in_further_messages_with_the_buttons_on_the_last() -> None:
    reply = Reply("a" * (TEXT_LIMIT + 10), (Button("Menu", "menu"),), code="vpn://" + "b" * 100)

    messages = messages_of(reply)

    assert [len(m["content"]) for m in messages] == [4 + 106 + 4, TEXT_LIMIT, 10]
    assert ["view" in m for m in messages] == [False, False, True]


@pytest.mark.asyncio
async def test_a_long_code_gets_messages_of_its_own() -> None:
    messages = messages_of(Reply("Your key", code="vpn://" + "b" * TEXT_LIMIT))

    assert messages[0]["content"].startswith("```\nvpn://")
    assert messages[-1]["content"] == "Your key"
    assert all(len(m["content"]) <= TEXT_LIMIT for m in messages)


@pytest.mark.asyncio
async def test_buttons_are_capped_and_links_open_the_address() -> None:
    buttons = (Button("FAQ", url=f"{SITE}/faq"), *(Button(str(n), f"a {n}") for n in range(30)))

    (message,) = messages_of(Reply("Menu", buttons))

    children = message["view"].children
    assert len(children) == BUTTON_LIMIT
    assert (children[0].url, children[0].custom_id) == (f"{SITE}/faq", None)
    assert isinstance(children[1], ActionButton)


def test_every_menu_item_is_a_slash_command_and_link_takes_a_code() -> None:
    commands = slash_commands(cast(Any, None))

    assert [c.name for c in commands] == [item.name for item in MENU] + ["link"]
    assert [p.name for p in commands[-1].parameters] == ["code"]
    assert commands[2].description == "My keys"


def test_only_the_menu_and_linking_are_offered_on_servers() -> None:
    commands = slash_commands(cast(Any, None))

    contexts = [(c.name, c.allowed_contexts) for c in commands]
    assert [name for name, where in contexts if where and where.guild] == ["menu", "link"]
    assert all(where and where.dm_channel for _, where in contexts)


@pytest.mark.asyncio
async def test_command_descriptions_are_translated_to_the_panel_languages() -> None:
    translator = MenuTranslator()
    context: app_commands.TranslationContextTypes = app_commands.TranslationContext(
        app_commands.TranslationContextLocation.other, None
    )
    string = app_commands.locale_str("My keys")

    russian = await translator.translate(string, discord.Locale.russian, context)

    with translation.override("ru"):
        assert russian == gettext("My keys")
    assert await translator.translate(string, discord.Locale.american_english, context) is None
    assert await translator.translate(string, discord.Locale.japanese, context) is None


@pytest.fixture
def anna() -> None:
    """Anna's Discord linked to a user with a plan, committed for the core's own thread."""
    LinkedAccount.objects.create(
        user=subscriber(max_keys=2), messenger=Messenger.DISCORD, external_id=str(ANNA_ID)
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_new_key_from_a_server_command_to_the_files_in_direct_messages(
    awg_server: object, anna: None
) -> None:
    adapter = DiscordAdapter(BotCore(links=SiteLinks(SITE)))
    on_server = _interaction(on_server=True)
    in_dm = _interaction()

    await adapter.on_command(on_server, "menu")
    await adapter.on_button(in_dm, "newkey")
    await adapter.on_button(in_dm, f"newkey {SOURCE} android")
    key = await AccessKey.objects.aget()
    await adapter.on_button(in_dm, f"show {key.pk}")

    assert [message["ephemeral"] for message in on_server.sent] == [True]
    assert "view" in on_server.user.dm[-1]  # the menu panel came to the direct messages
    # The menu became the device choice, then «issuing»; under the key it lost its buttons.
    assert len(in_dm.edits) == 3 and in_dm.edits[-1] == {"view": None}
    code, files, panel = in_dm.sent[-3:]
    assert code["content"].startswith("```\nvpn://")
    names = [file.filename for file in files["files"]]
    assert names[-1] == "Android.conf"
    assert any(name.endswith(".png") for name in names)
    assert "Android" in panel["content"] and "view" in panel


@dataclass
class FakeAttachment:
    filename: str
    content_type: str | None
    content: bytes = b"\x89PNG"

    async def read(self) -> bytes:
        return self.content


@pytest.mark.asyncio
async def test_an_image_in_a_direct_message_goes_to_the_core() -> None:
    core = StubCore(Reply("Sent."))
    files = [FakeAttachment("notes.txt", "text/plain"), FakeAttachment("shot.png", "image/png")]

    await DiscordAdapter(core).on_message(cast(Any, FakeMessage("", FakeDm(), attachments=files)))

    (incoming,) = core.received
    assert incoming.picture == Attachment("shot.png", b"\x89PNG", "image/png")


@pytest.mark.asyncio
async def test_a_popup_is_an_ephemeral_note_and_the_panel_stays() -> None:
    interaction = _interaction()

    await DiscordAdapter(StubCore(Reply("Wait.", placement=Placement.POPUP))).on_button(
        interaction, "keys"
    )

    assert interaction.edits == []
    assert interaction.sent == [{"content": "Wait.", "ephemeral": True}]


@pytest.mark.asyncio
async def test_a_new_panel_below_takes_the_buttons_off_the_pressed_one() -> None:
    interaction = _interaction()
    reply = Reply("Your key", code="vpn://abc").placed(Placement.NEW, clear_pressed=True)

    await DiscordAdapter(StubCore(reply)).on_button(interaction, "show 12")

    assert interaction.edits == [{"view": None}]
    assert interaction.sent[-1]["content"] == "Your key"
