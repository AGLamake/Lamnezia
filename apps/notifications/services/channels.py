"""Notification channels: one class per kind, registered by kind (CODE_STYLE.md 3).

A new kind is a `NotificationChannel` subclass with `@register` and a form; nothing else changes.
"""

import hashlib
import hmac
import json
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Self
from urllib.parse import urlsplit

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from apps.accounts.services.link_offer import LetterOffer
from apps.core.services.outbound import TelegramApi
from apps.core.services.site_settings import cached_settings
from apps.notifications.models import Channel, ChannelKind
from apps.notifications.services.addresses import (
    AddressGuard,
    BlockedAddressError,
    UnresolvedHostError,
)

SIGNATURE_HEADER = "X-Lamnezia-Signature"
# Answers worth another try later: timeout, rate limit and any server error.
_RETRYABLE_STATUS = frozenset({408, 425, 429})
_SERVER_ERROR = 500
_ERROR_DETAIL_LIMIT = 200
# Discord embed limits.
_EMBED_TITLE_LIMIT = 256
_EMBED_TEXT_LIMIT = 4096
_DM_TEXT_LIMIT = 2000
_TELEGRAM_TEXT_LIMIT = 4096
# The bot's «Menu» button in a notification: the action the bot core runs on a press, and the
# custom_id of a Discord action button (apps/bots/adapters/discord.py ActionButton).
_MENU_ACTION = "menu"
_DISCORD_MENU_ID = f"lamnezia:{_MENU_ACTION}"
# Discord message components: an action row, a button and its grey style.
_DISCORD_ROW, _DISCORD_BUTTON, _DISCORD_SECONDARY = 1, 2, 2


class DeliveryError(Exception):
    """The channel did not take the message. `retryable`: a later attempt may succeed;
    `status`: the HTTP status of the answer, when there was one.
    """

    def __init__(self, detail: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(detail)
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True)
class Message:
    """A notification as channels send it: text for people, `payload()` for programs.

    `offer`: a letter to the user also offers to link a messenger; other channels ignore it.
    """

    uid: str
    event: str
    created_at: datetime
    title: str
    text: str
    data: dict[str, Any]
    offer: LetterOffer | None = None

    @property
    def link(self) -> str:
        """The page the notification points to (`data['url']`); empty when there is none."""
        return str(self.data.get("url") or "")

    @property
    def summary(self) -> str:
        """The text without the link line that `EventType.render` puts at its end."""
        return self.text.removesuffix(f"\n{self.link}") if self.link else self.text

    def payload(self) -> dict[str, Any]:
        """The webhook body; its schema is documented and stable."""
        return {
            "id": self.uid,
            "event": self.event,
            "created_at": self.created_at.isoformat(),
            "data": self.data,
        }


def sign(secret: str, body: bytes) -> str:
    """The `X-Lamnezia-Signature` value: HMAC-SHA256 of the raw body."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is answered as an error: it could lead to an address the guard refuses."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class HttpClient:
    """POSTs JSON. Errors never carry the URL: for Telegram and Discord it holds the token.

    With a guard, addresses in private networks are refused, the connection goes only to the
    addresses the guard checked, and redirects are not followed. A request to the Bot API (`api`)
    goes to the panel's own setting, through its proxy, without the guard.
    """

    def __init__(self, timeout: float, guard: AddressGuard | None = None) -> None:
        self._timeout = timeout
        self._guard = guard

    @classmethod
    def from_settings(cls) -> Self:
        return cls(settings.NOTIFICATION_TIMEOUT, AddressGuard.from_settings())

    def post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str] | None = None,
        *,
        api: TelegramApi | None = None,
    ) -> bytes:
        """The answer body."""
        if urlsplit(url).scheme not in ("http", "https"):
            raise DeliveryError("only http and https addresses are allowed", retryable=False)
        open_url = self._opener(url) if api is None else api.opener(_RefuseRedirects())
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Lamnezia",
                **(headers or {}),
            },
        )
        try:
            with open_url(request, timeout=self._timeout) as response:
                answer: bytes = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read(_ERROR_DETAIL_LIMIT).decode(errors="replace")
            retryable = error.code >= _SERVER_ERROR or error.code in _RETRYABLE_STATUS
            raise DeliveryError(
                f"HTTP {error.code} {detail}".strip(), retryable=retryable, status=error.code
            ) from None
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise DeliveryError(f"connection failed: {reason}", retryable=True) from None
        return answer

    def _opener(self, url: str) -> Callable[..., Any]:
        """How to open the URL: with a guard, only to its checked addresses, without redirects."""
        if self._guard is None:
            return urllib.request.urlopen
        try:
            pinned = self._guard.pin(url)
        except BlockedAddressError as error:
            raise DeliveryError(
                f"{error.address} is not a public address; private networks are off "
                "in the panel settings",
                retryable=False,
            ) from None
        except UnresolvedHostError:
            raise DeliveryError(
                "connection failed: the name does not resolve", retryable=True
            ) from None
        if pinned is None:
            return urllib.request.build_opener(_RefuseRedirects).open
        return pinned.opener(_RefuseRedirects()).open


def _json(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


class NotificationChannel(ABC):
    """Sends a message to one place.

    `send` raises `DeliveryError` when the place did not take it; it never retries by itself.
    """

    kind: ClassVar[ChannelKind]

    @classmethod
    @abstractmethod
    def from_model(cls, channel: Channel, http: HttpClient) -> Self: ...

    @abstractmethod
    def send(self, message: Message) -> None: ...


_REGISTRY: dict[str, type[NotificationChannel]] = {}


def register[T: type[NotificationChannel]](channel_class: T) -> T:
    _REGISTRY[channel_class.kind] = channel_class
    return channel_class


def build_channel(channel: Channel, http: HttpClient) -> NotificationChannel:
    return _REGISTRY[channel.kind].from_model(channel, http)


@register
class EmailChannel(NotificationChannel):
    """A letter: the plain text, with an HTML version in the frame of email/base.html where the
    link of the event and the offer to link a messenger are buttons.
    """

    kind = ChannelKind.EMAIL
    TEMPLATE = "notifications/email/message.html"

    def __init__(self, recipients: list[str]) -> None:
        self._recipients = recipients

    @classmethod
    def from_model(cls, channel: Channel, http: HttpClient) -> Self:
        return cls(parse_addresses(channel.target))

    def send(self, message: Message) -> None:
        if not self._recipients:
            raise DeliveryError("no email address", retryable=False)
        letter = EmailMultiAlternatives(message.title, self._text(message), None, self._recipients)
        letter.attach_alternative(self._html(message), "text/html")
        try:
            letter.send()
        # SMTP errors and connection failures are OSError.
        except OSError as error:
            raise DeliveryError(f"mail server: {error}", retryable=True) from None

    @staticmethod
    def _text(message: Message) -> str:
        offer = message.offer
        return f"{message.text}\n\n{offer.line()}" if offer else message.text

    def _html(self, message: Message) -> str:
        return render_to_string(
            self.TEMPLATE,
            {
                "site_name": cached_settings.get().site_name,
                "title": message.title,
                "text": message.summary,
                "link": message.link,
                "offer": message.offer,
            },
        )


def parse_addresses(text: str) -> list[str]:
    return [address for address in re.split(r"[\s,;]+", text) if address]


@register
class TelegramChannel(NotificationChannel):
    """A message from a bot to a chat; without its own token the channel uses the panel bot.

    With a `menu` label the message has the bot's «Menu» button: a user's chat with the panel
    bot opens the menu from the notification. `api`: the Bot API server and proxy, by default
    from the settings.
    """

    kind = ChannelKind.TELEGRAM

    def __init__(
        self,
        token: str,
        chat_id: str,
        http: HttpClient,
        menu: str = "",
        api: TelegramApi | None = None,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._http = http
        self._menu = menu
        self._api = api or TelegramApi.from_settings()

    @classmethod
    def from_model(cls, channel: Channel, http: HttpClient) -> Self:
        token = channel.secret or cached_settings.get().telegram_bot_token
        return cls(token, channel.target, http)

    def send(self, message: Message) -> None:
        if not self._token:
            raise DeliveryError("no Telegram bot token", retryable=False)
        body: dict[str, object] = {
            "chat_id": self._chat_id,
            "text": f"{message.title}\n\n{message.text}"[:_TELEGRAM_TEXT_LIMIT],
            "disable_web_page_preview": True,
        }
        if self._menu:
            button = {"text": self._menu, "callback_data": _MENU_ACTION}
            body["reply_markup"] = {"inline_keyboard": [[button]]}
        url = self._api.method_url(self._token, "sendMessage")
        self._http.post(url, _json(body), api=self._api)


@register
class DiscordChannel(NotificationChannel):
    """A message as an embed through the webhook of a Discord channel."""

    kind = ChannelKind.DISCORD

    def __init__(self, webhook_url: str, http: HttpClient) -> None:
        self._webhook_url = webhook_url
        self._http = http

    @classmethod
    def from_model(cls, channel: Channel, http: HttpClient) -> Self:
        return cls(channel.secret, http)

    def send(self, message: Message) -> None:
        embed = {
            "title": message.title[:_EMBED_TITLE_LIMIT],
            "description": message.text[:_EMBED_TEXT_LIMIT],
            "timestamp": message.created_at.isoformat(),
            "footer": {"text": message.event},
        }
        self._http.post(self._webhook_url, _json({"username": "Lamnezia", "embeds": [embed]}))


@register
class DiscordDmChannel(NotificationChannel):
    """A direct message from the panel bot to a Discord user: the bot opens the DM, then posts.

    Discord takes a DM only from a bot that shares a server with the user. With a `menu` label
    the message has the bot's «Menu» button.
    """

    kind = ChannelKind.DISCORD_DM
    API_URL = "https://discord.com/api/v10"
    # Discord asks bots for this form of User-Agent.
    USER_AGENT = "DiscordBot (https://github.com/AGLamake/Lamnezia, 1)"

    def __init__(self, token: str, user_id: str, http: HttpClient, menu: str = "") -> None:
        self._token = token
        self._user_id = user_id
        self._http = http
        self._menu = menu

    @classmethod
    def from_model(cls, channel: Channel, http: HttpClient) -> Self:
        return cls(cached_settings.get().discord_bot_token, channel.target, http)

    def send(self, message: Message) -> None:
        if not self._token:
            raise DeliveryError("no Discord bot token", retryable=False)
        headers = {"Authorization": f"Bot {self._token}", "User-Agent": self.USER_AGENT}
        opened = self._http.post(
            f"{self.API_URL}/users/@me/channels",
            _json({"recipient_id": self._user_id}),
            headers,
        )
        try:
            channel_id = json.loads(opened)["id"]
        except (ValueError, KeyError, TypeError):
            raise DeliveryError("Discord did not open the direct message", retryable=True) from None
        text = f"**{message.title}**\n\n{message.text}"[:_DM_TEXT_LIMIT]
        body: dict[str, object] = {"content": text, "allowed_mentions": {"parse": []}}
        if self._menu:
            button = {
                "type": _DISCORD_BUTTON,
                "style": _DISCORD_SECONDARY,
                "label": self._menu,
                "custom_id": _DISCORD_MENU_ID,
            }
            body["components"] = [{"type": _DISCORD_ROW, "components": [button]}]
        self._http.post(f"{self.API_URL}/channels/{channel_id}/messages", _json(body), headers)


@register
class WebhookChannel(NotificationChannel):
    """The JSON payload, signed with the channel secret."""

    kind = ChannelKind.WEBHOOK

    def __init__(self, url: str, secret: str, http: HttpClient) -> None:
        self._url = url
        self._secret = secret
        self._http = http

    @classmethod
    def from_model(cls, channel: Channel, http: HttpClient) -> Self:
        return cls(channel.target, channel.secret, http)

    def send(self, message: Message) -> None:
        body = _json(message.payload())
        headers = {
            SIGNATURE_HEADER: sign(self._secret, body),
            "X-Lamnezia-Event": message.event,
            "X-Lamnezia-Delivery": message.uid,
        }
        self._http.post(self._url, body, headers)
