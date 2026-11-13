"""The linked user a command runs for, and the base of the command sets."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from django.utils.translation import gettext as _

from apps.accounts.models import Messenger, User
from apps.bots.services.replies import Button, Command, Reply
from apps.bots.services.session import Dialogs, SiteLinks

Handler = Callable[[Command], Reply]


@dataclass(frozen=True)
class Chat:
    """A linked messenger account and its panel user."""

    user: User
    messenger: Messenger
    sender: str
    links: SiteLinks
    dialogs: Dialogs

    @property
    def client(self) -> str:
        """What the user writes from, for the context of their requests to the staff."""
        return f"{self.messenger.label} bot"

    def wait_for_text(self, command: Command) -> None:
        """The user's next typed message finishes `command` as its `text`."""
        self.dialogs.wait(self.sender, command)


class CommandSet(ABC):
    """Commands of one topic. `handlers` maps a command name to the method that answers it.

    Every handler gets the parsed command and returns one reply; it calls the same services
    as the web pages and never trusts an argument: ids are looked up among the user's own.
    A section's reply has a title and ends with `nav()`: «Back» where there is a section above.
    """

    # Commands a user waiting for approval or blocked may run (DESIGN.md 6.6, 6.8).
    open_to_all: frozenset[str] = frozenset()

    def __init__(self, chat: Chat) -> None:
        self.chat = chat

    @property
    def user(self) -> User:
        return self.chat.user

    @abstractmethod
    def handlers(self) -> Mapping[str, Handler]: ...


def menu_button() -> Button:
    return Button(_("Menu"), "menu")


def back_button(action: str) -> Button:
    return Button(_("Back"), action)


def nav(back: str = "") -> tuple[Button, ...]:
    """The last buttons of a section: «Back» to the section it came from, then «Menu»."""
    return (back_button(back), menu_button()) if back else (menu_button(),)


def bullets(lines: Iterable[str]) -> str:
    """A tidy list: one item a line."""
    return "\n".join(f"• {line}" for line in lines)


def number(value: str) -> int | None:
    """An id from a command argument; None for anything else."""
    return int(value) if value.isdecimal() else None


def unavailable() -> Reply:
    return Reply(_("This is not available to you."), nav())
