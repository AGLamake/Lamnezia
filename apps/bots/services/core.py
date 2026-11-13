"""The bot core: one entry point every messenger adapter calls with what the user sent.

It knows nothing of Telegram or Discord. It answers in the user's language, limits how often
one account may send commands, shows nothing to a messenger account that is not linked
(except how to link it), and writes every command of a linked user to the audit journal.
It tells the adapter whether to redraw the panel in place or to post a new one. When the panel
is not on a public domain, links go in the text: messengers refuse such link buttons.
"""

from collections.abc import Mapping
from dataclasses import replace

from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext as _

from apps.accounts.services.linking import AccountLinks, LinkError
from apps.audit.services import journal as audit
from apps.bots.commands.account import AccountCommands
from apps.bots.commands.admin import REPLY, AdminCommands, replied_number
from apps.bots.commands.base import Chat, CommandSet, Handler, menu_button
from apps.bots.commands.keys import KeyCommands
from apps.bots.commands.servers import ServerCommands
from apps.bots.commands.support import CHAT, SupportCommands
from apps.bots.services.replies import Button, Command, Incoming, Placement, Reply
from apps.bots.services.session import Dialogs, Panels, SiteLinks, Throttle, sender_of

COMMAND_SETS: tuple[type[CommandSet], ...] = (
    AccountCommands,
    KeyCommands,
    ServerCommands,
    SupportCommands,
    AdminCommands,
)
# Commands that link a messenger account with a code from the profile (`/start <code>` is the
# Telegram deep link, `/link <code>` the Discord fallback).
_LINK_COMMANDS = frozenset({"start", "link"})


class BotCore:
    def __init__(
        self,
        *,
        links: SiteLinks | None = None,
        throttle: Throttle | None = None,
        dialogs: Dialogs | None = None,
        account_links: AccountLinks | None = None,
        panels: Panels | None = None,
    ) -> None:
        self._links = links or SiteLinks()
        self._throttle = throttle or Throttle()
        self._dialogs = dialogs or Dialogs()
        self._account_links = account_links or AccountLinks()
        self._panels = panels or Panels()

    def handle(self, incoming: Incoming) -> Reply:
        with translation.override(language_of(incoming.locale)):
            if not self._throttle.allow(sender_of(incoming)):
                return _too_many(incoming)
            reply = self._answer(incoming)
        placement = self._placement(incoming, reply)
        reply = reply.placed(
            placement, clear_pressed=bool(incoming.action) and placement == Placement.NEW
        )
        return reply if self._links.public else _links_in_text(reply)

    def _answer(self, incoming: Incoming) -> Reply:
        sender = sender_of(incoming)
        command = Command.of(incoming)
        user = self._account_links.user_of(incoming.messenger, incoming.external_id)
        if user is None or (command.name in _LINK_COMMANDS and command.args):
            return self._link(incoming, command)
        chat = Chat(user, incoming.messenger, sender, self._links, self._dialogs)
        return self._run(chat, self._resolve(chat, command, incoming.replied))

    def _placement(self, incoming: Incoming, reply: Reply) -> Placement:
        """A pressed button redraws its panel. Typed text, a key or files, and a notification
        that went under the panel open the panel anew at the bottom of the chat.
        """
        buried = self._panels.take_buried(sender_of(incoming))
        if not incoming.action or reply.has_attachments or buried:
            return Placement.NEW
        return Placement.REPLACE

    def _resolve(self, chat: Chat, command: Command, replied: str = "") -> Command:
        """A typed message finishes the waiting dialog step or goes to support; a command
        cancels the waiting step. A staff member's reply to a support notification answers
        the user.
        """
        if not command.is_text:
            self._dialogs.end(chat.sender)
            return command
        answered = replied_number(replied) if chat.user.is_staff_member else None
        if answered is not None:
            return Command(REPLY, (str(answered),), command.text)
        waiting = self._dialogs.take(chat.sender)
        if waiting is None:
            return Command(CHAT, text=command.text, picture=command.picture)
        return Command(waiting.name, waiting.args, command.text, command.picture)

    def _run(self, chat: Chat, command: Command) -> Reply:
        sets = [command_set(chat) for command_set in COMMAND_SETS]
        handlers: dict[str, Handler] = {}
        allowed: set[str] = set()
        for command_set in sets:
            handlers.update(command_set.handlers())
            allowed |= command_set.open_to_all
        handler = handlers.get(command.name)
        if handler is None:
            return Reply(_("I do not know this command."), (menu_button(),))
        if not chat.user.can_have_keys and command.name not in allowed:
            return Reply(_("This is not available until your account is approved."))
        _journal(chat, command.name)
        return handler(command)

    def _link(self, incoming: Incoming, command: Command) -> Reply:
        """Link the account with a code; without one, tell how to get it."""
        code = command.arg(0)
        if command.name not in _LINK_COMMANDS or not code:
            return _not_linked(self._links)
        try:
            account = self._account_links.redeem(
                incoming.messenger, code, incoming.external_id, incoming.username
            )
        except LinkError as error:
            return Reply(str(error), (_profile_button(self._links),))
        audit.log(
            "accounts.link",
            actor=account.user,
            target=account,
            data={"messenger": incoming.messenger, "via": "bot"},
        )
        return Reply(
            _("Linked to %(user)s on the panel.") % {"user": account.user.get_username()},
            (menu_button(),),
        )


def language_of(locale: str) -> str:
    """The panel language closest to the messenger's ("en-US" → "en"), else the default one."""
    code = locale.replace("_", "-").partition("-")[0].lower()
    known: Mapping[str, str] = dict(settings.LANGUAGES)
    return code if code in known else settings.LANGUAGE_CODE


def _journal(chat: Chat, name: str) -> None:
    """Only the command's name: arguments and typed text never go to the journal."""
    audit.log(
        "bots.command",
        actor=chat.user,
        target=chat.user,
        data={"messenger": chat.messenger, "command": name},
    )


def _too_many(incoming: Incoming) -> Reply:
    """A pressed button gets a popup and keeps its panel; typed text gets a message."""
    text = _("Too many commands. Please wait a minute.")
    if incoming.action:
        return Reply(text, placement=Placement.POPUP)
    return Reply(text, (menu_button(),))


def _links_in_text(reply: Reply) -> Reply:
    """The link buttons as lines under the text, the other buttons as they are."""
    links = [f"{button.label}: {button.url}" for button in reply.buttons if button.url]
    if not links:
        return reply
    return replace(
        reply,
        text="\n\n".join([reply.text, "\n".join(links)]),
        buttons=tuple(button for button in reply.buttons if not button.url),
    )


def _not_linked(links: SiteLinks) -> Reply:
    return Reply(
        _(
            "This account is not linked to the panel. Open Messengers in your profile on the "
            "panel and press Link account."
        ),
        (_profile_button(links),),
    )


def _profile_button(links: SiteLinks) -> Button:
    return Button(_("Open the profile"), url=links.to("accounts:linked"))
