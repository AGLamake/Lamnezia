"""Who the visitor is in Telegram or Discord: the Telegram login check and Discord OAuth2."""

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from django.utils import timezone


class MessengerAuthError(Exception):
    """The messenger did not confirm who the visitor is."""


@dataclass(frozen=True)
class MessengerIdentity:
    external_id: str
    username: str


class TelegramLoginCheck:
    """Checks the login data Telegram passes to the panel.

    https://core.telegram.org/widgets/login#checking-authorization: the hash is an HMAC-SHA256 of
    the sorted fields keyed with SHA-256 of the bot token; `auth_date` limits a replay.
    """

    def __init__(
        self,
        bot_token: str,
        *,
        max_age: timedelta,
        clock: Callable[[], datetime] = timezone.now,
    ) -> None:
        self._secret = hashlib.sha256(bot_token.encode()).digest()
        self._max_age = max_age
        self._clock = clock

    def identity(self, data: Mapping[str, str]) -> MessengerIdentity:
        fields = {name: value for name, value in data.items() if name != "hash"}
        check = "\n".join(f"{name}={fields[name]}" for name in sorted(fields))
        expected = hmac.new(self._secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected.encode(), data.get("hash", "").encode()):
            raise MessengerAuthError("wrong Telegram signature")
        try:
            signed_at = datetime.fromtimestamp(int(fields["auth_date"]), tz=UTC)
            external_id = str(int(fields["id"]))
        except (KeyError, ValueError, OverflowError, OSError):
            raise MessengerAuthError("incomplete Telegram data") from None
        if self._clock() - signed_at > self._max_age:
            raise MessengerAuthError("the Telegram login is too old")
        return MessengerIdentity(external_id, fields.get("username", ""))


TELEGRAM_OAUTH_URL = "https://oauth.telegram.org/auth"


def telegram_login_url(bot_token: str, origin: str, return_to: str) -> str:
    """Telegram's own login page for the bot; it returns the visitor to `return_to` with the
    signed data in the `#tgAuthResult` fragment. The bot id is the token's part before ":";
    `origin` must be the domain set with /setdomain in @BotFather.
    """
    query = urlencode(
        {
            "bot_id": bot_token.partition(":")[0],
            "origin": origin,
            "return_to": return_to,
            "request_access": "write",
        }
    )
    return f"{TELEGRAM_OAUTH_URL}?{query}"


# Sends a request and returns the parsed JSON answer; tests replace it.
Transport = Callable[[urllib.request.Request], Any]


def _urlopen_json(request: urllib.request.Request, timeout: float = 10) -> Any:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise MessengerAuthError(f"Discord did not answer: {error}") from None


class DiscordOAuth:
    """The OAuth2 code flow with the `identify` scope: who the visitor is in Discord."""

    AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
    TOKEN_URL = "https://discord.com/api/v10/oauth2/token"
    USER_URL = "https://discord.com/api/v10/users/@me"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        transport: Transport = _urlopen_json,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._transport = transport

    def authorize_url(self, state: str) -> str:
        query = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": "identify",
            "state": state,
            "prompt": "none",
        }
        return f"{self.AUTHORIZE_URL}?{urlencode(query)}"

    def identity(self, code: str) -> MessengerIdentity:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        token = self._transport(
            urllib.request.Request(
                self.TOKEN_URL,
                data=urlencode(form).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded", **_AGENT},
            )
        )
        if not isinstance(token, dict) or "access_token" not in token:
            raise MessengerAuthError("Discord gave no access token")
        user = self._transport(
            urllib.request.Request(
                self.USER_URL,
                headers={"Authorization": f"Bearer {token['access_token']}", **_AGENT},
            )
        )
        if not isinstance(user, dict) or "id" not in user:
            raise MessengerAuthError("Discord gave no user")
        return MessengerIdentity(str(user["id"]), str(user.get("username", "")))


# Discord refuses requests without a User-Agent of its own format.
_AGENT = {"User-Agent": "DiscordBot (https://github.com/AGLamake/Amnezia-Web, 1.0)"}
