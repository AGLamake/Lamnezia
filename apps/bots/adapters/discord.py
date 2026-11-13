"""The Discord bot (discord.py, gateway; DESIGN.md 6.8).

Users work with it in a direct message: slash commands, buttons and typed text (dialog steps,
support, with an image attached). On a server only `/menu` and `/link` are offered; the answer
goes to the user's direct messages and the server gets an ephemeral note about it, or a hint how
to open direct messages when they are closed. Nothing the bot says is seen by others on the
server, and plain messages on servers are not read at all.
"""

import io
from contextlib import suppress
from typing import Any, cast

import discord
from discord import app_commands
from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext, gettext_noop
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Messenger
from apps.bots.adapters.base import BotAdapter, BotServer, adapters
from apps.bots.services.core import language_of
from apps.bots.services.menu import MENU
from apps.bots.services.replies import (
    Attachment,
    Button,
    Incoming,
    Placement,
    Reply,
)
from apps.core.models import SiteSettings

# Discord limits: message text, buttons on one message, button label, custom_id.
TEXT_LIMIT = 2000
BUTTON_LIMIT = 25
LABEL_LIMIT = 80
ACTION_PREFIX = "lamnezia:"
_CUSTOM_ID_LIMIT = 100
LINK_COMMAND = "link"
# Commands offered on servers; the others work in a direct message only.
SERVER_COMMANDS = frozenset({"menu", LINK_COMMAND})
WROTE_TO_DM = _("I wrote to you in direct messages.")
DM_CLOSED = _(
    "I can't write to you: your direct messages are closed. Open the server menu, go to "
    "Privacy Settings, allow direct messages from server members and repeat the command."
)


class ActionButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=ACTION_PREFIX + r"(?P<action>.+)",
):
    """A button that runs a core action. It works after a restart of the bot as well:
    discord.py finds it by its custom_id, without keeping the sent message in memory.
    """

    def __init__(self, action: str, label: str = "") -> None:
        super().__init__(
            discord.ui.Button(
                label=label[:LABEL_LIMIT] or "…",
                custom_id=(ACTION_PREFIX + action)[:_CUSTOM_ID_LIMIT],
                style=discord.ButtonStyle.secondary,
            )
        )
        self.action = action

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction[Any], item: discord.ui.Item[Any], match: Any, /
    ) -> "ActionButton":
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        client = cast(DiscordClient, interaction.client)
        await client.adapter.on_button(interaction, self.action)


@adapters.register
class DiscordAdapter(BotAdapter):
    messenger = Messenger.DISCORD

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.client = DiscordClient(self)

    @classmethod
    def token(cls) -> str:
        # Read anew each time: without a shared cache the panel's change would not reach here.
        token: str = SiteSettings.load().discord_bot_token
        return token

    async def run(self, token: str) -> None:
        async with self.client:
            await self.client.start(token)

    def servers(self) -> list[BotServer]:
        return [
            BotServer(str(guild.id), guild.name, guild.member_count or 0)
            for guild in self.client.guilds
        ]

    async def leave(self, server_id: str) -> None:
        guild = self.client.get_guild(int(server_id))
        if guild is not None:
            await guild.leave()

    async def on_message(self, message: discord.Message) -> None:
        """Typed text or an image in a direct message; messages on servers are never read."""
        if message.author.bot or not isinstance(message.channel, discord.DMChannel):
            return
        picture = await _picture(message)
        if not message.content and picture is None:
            return
        incoming = self._incoming(
            message.author, text=message.content, replied=_replied(message), picture=picture
        )
        reply = await self.answer(incoming)
        for part in messages_of(reply):
            await message.channel.send(**part)

    async def on_command(self, interaction: discord.Interaction[Any], line: str) -> None:
        # Discord waits 3 seconds for an answer; the core may take longer. On a server the
        # «thinking» note is seen by the user who asked only.
        await interaction.response.defer(ephemeral=_on_server(interaction), thinking=True)
        reply = await self.answer(self._incoming_of(interaction, text="/" + line))
        await self._post(interaction, reply)

    async def on_button(self, interaction: discord.Interaction[Any], action: str) -> None:
        # Deferred as an update of the message with the button: it may be redrawn later.
        await interaction.response.defer()
        reply = await self.answer(self._incoming_of(interaction, action=action))
        if reply.placement == Placement.POPUP:
            await interaction.followup.send(content=reply.text, ephemeral=True)
            return
        if reply.placement == Placement.REPLACE and await self._redraw(interaction, reply):
            return
        if reply.clear_pressed:
            await self._clear(interaction)
        await self._post(interaction, reply)

    async def _clear(self, interaction: discord.Interaction[Any]) -> None:
        """Take the buttons off the panel whose button was pressed."""
        with suppress(discord.HTTPException):
            await interaction.edit_original_response(view=None)

    async def _redraw(self, interaction: discord.Interaction[Any], reply: Reply) -> bool:
        """Whether the panel the button is on now shows the reply."""
        parts = panel_of(reply)
        if len(parts) > 1:  # a panel longer than one message is posted anew
            return False
        (part,) = parts
        try:
            await interaction.edit_original_response(
                content=part["content"], view=part.get("view"), attachments=[]
            )
        except discord.HTTPException:
            return False
        return True

    async def _post(self, interaction: discord.Interaction[Any], reply: Reply) -> None:
        if _on_server(interaction):
            await self._post_to_dm(interaction, reply)
            return
        for part in messages_of(reply):
            await interaction.followup.send(ephemeral=False, **part)

    async def _post_to_dm(self, interaction: discord.Interaction[Any], reply: Reply) -> None:
        """The reply in the user's direct messages; on the server, a note only they see."""
        try:
            for part in messages_of(reply):
                await interaction.user.send(**part)
        except discord.Forbidden:
            note = DM_CLOSED
        else:
            note = WROTE_TO_DM
        with translation.override(language_of(interaction.locale.value)):
            await interaction.followup.send(content=str(note), ephemeral=True)

    def _incoming_of(
        self, interaction: discord.Interaction[Any], *, text: str = "", action: str = ""
    ) -> Incoming:
        return self._incoming(
            interaction.user, text=text, action=action, locale=interaction.locale.value
        )

    def _incoming(  # noqa: PLR0913  (all but the sender are keyword-only parts of a message)
        self,
        sender: discord.User | discord.Member,
        *,
        text: str = "",
        action: str = "",
        locale: str = "",
        replied: str = "",
        picture: Attachment | None = None,
    ) -> Incoming:
        return Incoming(
            self.messenger,
            str(sender.id),
            sender.name,
            text=text,
            action=action,
            locale=locale,
            replied=replied,
            picture=picture,
        )


async def _picture(message: discord.Message) -> Attachment | None:
    """The first image attached to the message; other files are not taken."""
    for item in message.attachments:
        if (item.content_type or "").startswith("image/"):
            return Attachment(item.filename, await item.read(), item.content_type or "")
    return None


def _replied(message: discord.Message) -> str:
    """The text of the bot's message this one replies to; empty for anything else."""
    reference = message.reference
    original = reference.resolved if reference is not None else None
    if not isinstance(original, discord.Message) or not original.author.bot:
        return ""
    return original.content


def _on_server(interaction: discord.Interaction[Any]) -> bool:
    return interaction.guild is not None


class DiscordClient(discord.Client):
    """The gateway connection with the slash commands of the menu and `/link <code>`."""

    def __init__(self, adapter: DiscordAdapter) -> None:
        super().__init__(intents=discord.Intents(guilds=True, dm_messages=True))
        self.adapter = adapter
        self.tree = app_commands.CommandTree(self)
        for command in slash_commands(adapter):
            self.tree.add_command(command)

    async def setup_hook(self) -> None:
        self.add_dynamic_items(ActionButton)
        await self.tree.set_translator(MenuTranslator())
        await self.tree.sync()

    async def on_ready(self) -> None:
        self.adapter.identity = str(self.user) if self.user else ""

    async def on_message(self, message: discord.Message) -> None:
        await self.adapter.on_message(message)


def slash_commands(adapter: DiscordAdapter) -> list[app_commands.Command[Any, Any, None]]:
    def menu_command(name: str, description: str) -> app_commands.Command[Any, Any, None]:
        async def callback(interaction: discord.Interaction[Any]) -> None:
            await adapter.on_command(interaction, name)

        return app_commands.Command(
            name=name,
            description=app_commands.locale_str(description),
            callback=callback,
            allowed_contexts=_contexts(name),
        )

    async def link(interaction: discord.Interaction[Any], code: str) -> None:
        await adapter.on_command(interaction, f"{LINK_COMMAND} {code}")

    with translation.override("en"):
        commands = [menu_command(item.name, str(item.description)) for item in MENU]
        commands.append(
            app_commands.Command(
                name=LINK_COMMAND,
                description=app_commands.locale_str(
                    gettext_noop("Link this account with a code from your profile")
                ),
                callback=link,
                allowed_contexts=_contexts(LINK_COMMAND),
            )
        )
    return commands


def _contexts(name: str) -> app_commands.AppCommandContext:
    """Where Discord offers the command: a direct message, and a server for a few."""
    return app_commands.AppCommandContext(dm_channel=True, guild=name in SERVER_COMMANDS)


class MenuTranslator(app_commands.Translator):
    """Command descriptions in every panel language; Discord shows English to the others."""

    async def translate(
        self,
        string: app_commands.locale_str,
        locale: discord.Locale,
        context: app_commands.TranslationContextTypes,
    ) -> str | None:
        language = locale.value.partition("-")[0]
        if language == "en" or language not in dict(settings.LANGUAGES):
            return None
        with translation.override(language):
            return gettext(string.message)


def messages_of(reply: Reply) -> list[dict[str, Any]]:
    """The reply as new Discord messages: the `vpn://` link as a code block, the pictures and
    files in one message, then the panel below them.
    """
    parts: list[dict[str, Any]] = []
    if reply.code:
        parts.extend({"content": text} for text in _split(f"```\n{reply.code}\n```"))
    files = [
        discord.File(io.BytesIO(item.content), filename=item.name)
        for item in (*reply.pictures, *reply.files)
    ]
    if files:
        parts.append({"files": files})
    return [*parts, *panel_of(reply)]


def panel_of(reply: Reply) -> list[dict[str, Any]]:
    """The panel: the title in bold, the text and the buttons. Text over the limit goes on in
    further messages; the buttons go with the last one.
    """
    text = reply.text
    if reply.title:
        text = f"**{discord.utils.escape_markdown(reply.title)}**\n\n{text}"
    parts: list[dict[str, Any]] = [{"content": part} for part in _split(text)]
    view = view_of(reply.buttons)
    if view is not None:
        parts[-1]["view"] = view
    return parts


def _split(text: str) -> list[str]:
    text = text or "…"
    return [text[start : start + TEXT_LIMIT] for start in range(0, len(text), TEXT_LIMIT)]


def view_of(buttons: tuple[Button, ...]) -> discord.ui.View | None:
    if not buttons:
        return None
    view = discord.ui.View(timeout=None)
    for button in buttons[:BUTTON_LIMIT]:
        if button.url:
            view.add_item(discord.ui.Button(label=button.label[:LABEL_LIMIT], url=button.url))
        else:
            view.add_item(ActionButton(button.action, button.label))
    return view
