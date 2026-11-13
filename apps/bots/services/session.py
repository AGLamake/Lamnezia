"""What the bot keeps about a messenger account between messages: its command rate, the
dialog step it is in and whether its panel is buried under a notification. All of it lives in
the cache and expires on its own; nothing is secret.
"""

from dataclasses import dataclass
from datetime import timedelta
from ipaddress import ip_address
from urllib.parse import urlsplit

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from apps.accounts.models import Messenger
from apps.bots.services.replies import Command, Incoming


def sender_of(incoming: Incoming) -> str:
    return f"{incoming.messenger}:{incoming.external_id}"


class Throttle:
    """At most `limit` commands a minute from one sender (0: no limit)."""

    _WINDOW_SECONDS = 60

    def __init__(self, limit: int | None = None) -> None:
        self._limit = settings.BOT_RATE_LIMIT if limit is None else limit

    def allow(self, sender: str) -> bool:
        if not self._limit:
            return True
        key = f"bots:rate:{sender}"
        # add() starts the window only once; incr() is atomic, so parallel messages count right.
        cache.add(key, 0, timeout=self._WINDOW_SECONDS)
        try:
            count = cache.incr(key)
        except ValueError:  # the window expired between add() and incr()
            cache.set(key, 1, timeout=self._WINDOW_SECONDS)
            count = 1
        return count <= self._limit


class Dialogs:
    """The step that waits for the user's next typed message, as the command to finish it."""

    def __init__(self, lifetime: timedelta | None = None) -> None:
        self._lifetime = lifetime or timedelta(minutes=settings.BOT_DIALOG_MINUTES)

    def wait(self, sender: str, command: Command) -> None:
        cache.set(self._key(sender), [command.name, *command.args], self._lifetime.total_seconds())

    def take(self, sender: str) -> Command | None:
        """The waiting step, which ends now; None when there is none."""
        stored = cache.get(self._key(sender))
        cache.delete(self._key(sender))
        return Command(stored[0], tuple(stored[1:])) if stored else None

    def end(self, sender: str) -> None:
        cache.delete(self._key(sender))

    @staticmethod
    def _key(sender: str) -> str:
        return f"bots:dialog:{sender}"


class Panels:
    """Remembers that a message of the bot went under the user's panel (a notification), so the
    next panel opens anew at the bottom of the chat instead of redrawing the one above.

    The notifications worker marks it and the bot process takes it: they share the cache.
    """

    _LIFETIME_SECONDS = 7 * 24 * 3600

    def bury(self, messenger: Messenger, external_id: str) -> None:
        sender = sender_of(Incoming(messenger, external_id))
        cache.set(self._key(sender), True, self._LIFETIME_SECONDS)

    def take_buried(self, sender: str) -> bool:
        """Whether the panel was buried; the mark ends now, as the next panel is at the bottom."""
        key = self._key(sender)
        buried = bool(cache.get(key))
        if buried:
            cache.delete(key)
        return buried

    @staticmethod
    def _key(sender: str) -> str:
        return f"bots:panel-buried:{sender}"


@dataclass(frozen=True)
class SiteLinks:
    """Absolute links to pages of the panel, for buttons that open the browser."""

    base_url: str = ""
    # Names that never resolve on the internet: a dev machine or a home network.
    _LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")

    def to(self, view: str, *args: object) -> str:
        return self._site.rstrip("/") + reverse(view, args=args)

    @property
    def public(self) -> bool:
        """Whether the panel is on an internet domain or address. Telegram refuses a button
        that opens `localhost` or a name without a dot, so such links go in the text instead.
        """
        host = (urlsplit(self._site).hostname or "").rstrip(".")
        try:
            return ip_address(host).is_global
        except ValueError:
            return "." in host and not f".{host}".endswith(self._LOCAL_SUFFIXES)

    @property
    def _site(self) -> str:
        site: str = self.base_url or settings.SITE_URL
        return site
