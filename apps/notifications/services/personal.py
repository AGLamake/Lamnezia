"""The channels a notification to a user can go by: their chats with the bots and their email.

A messenger counts only when it is linked and its bot has a token (DESIGN.md 6.8). A message
in a chat with the bot has the bot's «Menu» button, in the active language.
"""

from dataclasses import dataclass

from django.utils.translation import gettext as _

from apps.accounts.models import LinkedAccount, NotifyVia, User
from apps.core.services.site_settings import cached_settings
from apps.notifications.services.channels import (
    DiscordDmChannel,
    EmailChannel,
    HttpClient,
    NotificationChannel,
    TelegramChannel,
)


@dataclass(frozen=True)
class Destination:
    """A channel to the user; `chat` is their chat with a bot, None for email."""

    channel: NotificationChannel
    chat: LinkedAccount | None = None

    @property
    def via(self) -> str:
        """The `NotifyVia` of this channel."""
        return self.chat.messenger if self.chat else NotifyVia.EMAIL


class PersonalChannels:
    """The user's destinations, the chosen one first."""

    ORDER = (NotifyVia.TELEGRAM, NotifyVia.DISCORD, NotifyVia.EMAIL)

    def __init__(self, user: User | None, http: HttpClient) -> None:
        self._user = user
        self._http = http

    def first(self, chosen: str) -> Destination:
        """The chosen messenger, else email."""
        return self._destination(chosen) or self._email()

    def all(self, chosen: str, skip: str = "") -> list[Destination]:
        """The chosen one, then the others in ORDER; the ones the user cannot get and `skip` are
        left out. Empty only when nothing but `skip` is left.
        """
        order = [via for via in (chosen, *self.ORDER) if via != skip]
        found = [self._destination(via) for via in dict.fromkeys(order)]
        destinations = [destination for destination in found if destination]
        if destinations or skip == NotifyVia.EMAIL:
            return destinations
        return [self._email()]

    def _destination(self, via: str) -> Destination | None:
        if via == NotifyVia.EMAIL:
            return self._email() if self._user and self._user.email else None
        if via not in (NotifyVia.TELEGRAM, NotifyVia.DISCORD) or self._user is None:
            return None
        site = cached_settings.get()
        if via == NotifyVia.TELEGRAM:
            token: str = site.telegram_bot_token
        else:
            token = site.discord_bot_token
        chat = self._user.linked_accounts.filter(messenger=via).first() if token else None
        if chat is None:
            return None
        menu = _("Menu")
        if via == NotifyVia.TELEGRAM:
            # A private chat with the bot has the user's id as its chat id.
            return Destination(TelegramChannel(token, chat.external_id, self._http, menu), chat)
        return Destination(DiscordDmChannel(token, chat.external_id, self._http, menu), chat)

    def _email(self) -> Destination:
        """Email even without an address: sending then fails with a clear error."""
        return Destination(
            EmailChannel([self._user.email] if self._user and self._user.email else [])
        )
