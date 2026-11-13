"""What the bot core gets from an adapter and gives back, the same for every messenger.

Buttons carry an action: a command in the same form a user types, without the slash
(`show 12 android`), so a pressed button and a typed command go the same way.
The bot talks through one panel message: a pressed button redraws it in place, anything else
posts it anew at the bottom of the chat (`Placement`).
"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Self

from apps.accounts.models import Messenger


@dataclass(frozen=True)
class Attachment:
    name: str
    content: bytes
    mime_type: str


@dataclass(frozen=True)
class Incoming:
    """A message or a pressed button. `locale` is the messenger's language code ("en-US").

    `action` is the action of the pressed button; empty for a typed message. `replied` is the
    text of the bot's own message the typed one replies to, if any. `picture` is an image the
    user sent; its caption is the `text`.
    """

    messenger: Messenger
    external_id: str
    username: str = ""
    text: str = ""
    action: str = ""
    locale: str = ""
    replied: str = ""
    picture: Attachment | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Command:
    """A command name with its arguments, or free `text` the user typed (an empty name),
    with the `picture` sent along with the text.
    """

    name: str
    args: tuple[str, ...] = ()
    text: str = ""
    picture: Attachment | None = field(default=None, repr=False)

    @classmethod
    def of(cls, incoming: Incoming) -> Self:
        if incoming.action:
            return cls.parse(incoming.action)
        text = incoming.text.strip()
        if text.startswith("/") and incoming.picture is None:
            return cls.parse(text[1:])
        return cls("", text=text, picture=incoming.picture)

    @classmethod
    def parse(cls, line: str) -> Self:
        """`start@my_bot code` → start, (code,): Telegram adds the bot name in group chats."""
        name, *args = line.split() or [""]
        return cls(name.split("@")[0].lower(), tuple(args))

    @property
    def is_text(self) -> bool:
        return not self.name

    def arg(self, index: int) -> str:
        return self.args[index] if index < len(self.args) else ""


@dataclass(frozen=True)
class Button:
    """A button that runs `action`, or opens `url` in the browser when it is set."""

    label: str
    action: str = ""
    url: str = ""


class Placement(StrEnum):
    """Where the adapter shows the panel of a reply."""

    REPLACE = "replace"  # redraw the panel whose button was pressed, in place
    NEW = "new"  # post the panel anew at the bottom of the chat
    POPUP = "popup"  # a short note over the pressed button; the panel stays as it is


@dataclass(frozen=True)
class Reply:
    """One answer of the bot: a panel (`title`, `text`, `buttons`) and what comes with it.

    `code` (a `vpn://` link, shown as copyable monospace text), `pictures` and `files` go as
    separate messages before the panel, so redrawing the panel never takes them away.
    A `private` reply holds a key: the adapter shows it only to the user who asked
    (a direct message, or an ephemeral answer in Discord) and never in a group chat.
    The core sets `placement` and `clear_pressed`; command handlers leave them alone.
    `clear_pressed`: a new panel opens below the one whose button was pressed, so the adapter
    takes the buttons off the old one.
    """

    text: str
    buttons: tuple[Button, ...] = ()
    code: str = ""
    pictures: tuple[Attachment, ...] = field(default=(), repr=False)
    files: tuple[Attachment, ...] = field(default=(), repr=False)
    private: bool = False
    title: str = ""
    placement: Placement = Placement.NEW
    clear_pressed: bool = False

    @property
    def has_attachments(self) -> bool:
        """The reply brings separate messages: a key link, pictures or files."""
        return bool(self.code or self.pictures or self.files)

    def placed(self, placement: Placement, *, clear_pressed: bool = False) -> Self:
        return replace(self, placement=placement, clear_pressed=clear_pressed)


def split_text(text: str, limit: int, length: Callable[[str], int] = len) -> list[str]:
    """The text in pieces of at most `limit` by `length`, cut at a line break where there is
    one: a messenger refuses a longer message. An empty text is one empty piece.
    """
    pieces: list[str] = []
    while length(text) > limit:
        end = _fitting(text, limit, length)
        cut = text.rfind("\n", 0, end + 1)
        if cut <= 0:
            cut = end
        pieces.append(text[:cut])
        text = text[cut:].removeprefix("\n")
    return [*pieces, text]


def _fitting(text: str, limit: int, length: Callable[[str], int]) -> int:
    """How many first characters of the text fit in `limit`."""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if length(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return low
