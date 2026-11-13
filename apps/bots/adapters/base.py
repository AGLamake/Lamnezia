"""The contract of a messenger adapter and the registry `manage.py runbots` starts them from."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from asgiref.sync import sync_to_async

from apps.accounts.models import Messenger
from apps.bots.services.core import BotCore
from apps.bots.services.replies import Incoming, Reply


@dataclass(frozen=True)
class BotServer:
    """A server (a Discord guild) the bot is a member of."""

    id: str
    name: str
    members: int


class BotAdapter(ABC):
    """Talks to one messenger: turns its updates into `Incoming`, shows each `Reply`.

    An adapter has no logic of its own. It must:
    - pass every message and pressed button to `answer` and show the reply as it is: the text,
      `code` as copyable monospace text, the buttons, `pictures` as images and `files` as files;
    - show a `private` reply only to the user who asked (a direct message or an ephemeral
      answer), never in a group chat;
    - set `Incoming.locale` from the messenger's language of the user when it knows it;
    - set `identity` (the bot's name, "@lamnezia_bot") once the messenger accepted the token.

    The supervisor (`services/supervisor.py`) starts `run` and cancels it to stop the bot.
    """

    messenger: Messenger

    def __init__(self, core: BotCore | None = None) -> None:
        self.core = core or BotCore()
        self.identity = ""

    @classmethod
    @abstractmethod
    def token(cls) -> str:
        """The bot token from the panel settings; empty while none is set."""

    @abstractmethod
    async def run(self, token: str) -> None:
        """Connect with `token` and receive updates until cancelled."""

    def servers(self) -> list[BotServer]:
        """The servers the bot is on; a messenger without servers (Telegram) has none."""
        return []

    async def leave(self, server_id: str) -> None:
        """Leave one of `servers()`; nothing to do for a messenger without servers."""
        return None

    async def answer(self, incoming: Incoming) -> Reply:
        # The core uses the Django ORM, which runs in a thread of its own outside the event loop.
        return await sync_to_async(self.core.handle)(incoming)


class AdapterRegistry:
    """Usage: ``@adapters.register`` on a `BotAdapter` subclass."""

    def __init__(self) -> None:
        self._classes: list[type[BotAdapter]] = []

    def register(self, adapter: type[BotAdapter]) -> type[BotAdapter]:
        self._classes.append(adapter)
        return adapter

    def unregister(self, adapter: type[BotAdapter]) -> None:
        self._classes.remove(adapter)

    def all(self) -> list[type[BotAdapter]]:
        return list(self._classes)


adapters = AdapterRegistry()
