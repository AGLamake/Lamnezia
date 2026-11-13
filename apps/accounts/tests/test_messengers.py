import hashlib
import hmac
import urllib.request
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.accounts.services.messengers import (
    DiscordOAuth,
    MessengerAuthError,
    TelegramLoginCheck,
    telegram_login_url,
)

BOT_TOKEN = "123:abc"


def telegram_data(token: str = BOT_TOKEN, **fields: str) -> dict[str, str]:
    """What the Telegram Login Widget sends, signed as Telegram signs it."""
    data = {"id": "1001", "username": "alice", "auth_date": str(int(timezone.now().timestamp()))}
    data.update(fields)
    check = "\n".join(f"{name}={data[name]}" for name in sorted(data))
    secret = hashlib.sha256(token.encode()).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return data


def check() -> TelegramLoginCheck:
    return TelegramLoginCheck(BOT_TOKEN, max_age=timedelta(minutes=10))


def test_telegram_data_signed_with_the_bot_token_is_accepted() -> None:
    identity = check().identity(telegram_data())

    assert (identity.external_id, identity.username) == ("1001", "alice")


@pytest.mark.parametrize(
    "data",
    [
        telegram_data(token="999:other"),
        {**telegram_data(), "id": "2002"},
        {**telegram_data(), "hash": "ф"},
        {key: value for key, value in telegram_data().items() if key != "hash"},
    ],
)
def test_telegram_data_with_a_wrong_signature_is_refused(data: dict[str, str]) -> None:
    with pytest.raises(MessengerAuthError):
        check().identity(data)


def test_the_telegram_login_page_is_asked_for_the_bot_of_the_token() -> None:
    url = telegram_login_url(BOT_TOKEN, "https://panel.example", "https://panel.example/back/")

    assert url == (
        "https://oauth.telegram.org/auth?bot_id=123&origin=https%3A%2F%2Fpanel.example"
        "&return_to=https%3A%2F%2Fpanel.example%2Fback%2F&request_access=write"
    )


def test_old_telegram_data_is_refused() -> None:
    old = str(int((timezone.now() - timedelta(minutes=11)).timestamp()))

    with pytest.raises(MessengerAuthError):
        check().identity(telegram_data(auth_date=old))


class FakeDiscord:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request) -> Any:
        self.requests.append(request)
        return self.answers.pop(0)


def oauth(transport: FakeDiscord) -> DiscordOAuth:
    return DiscordOAuth("42", "secret", "https://panel.test/accounts/discord/callback/", transport)


def test_discord_asks_only_for_identify() -> None:
    url = oauth(FakeDiscord()).authorize_url("st4te")

    assert "scope=identify&" in url
    assert "state=st4te" in url


def test_discord_code_is_exchanged_for_the_user() -> None:
    discord = FakeDiscord({"access_token": "t0ken"}, {"id": "555", "username": "bob"})

    identity = oauth(discord).identity("c0de")

    assert (identity.external_id, identity.username) == ("555", "bob")
    assert discord.requests[1].get_header("Authorization") == "Bearer t0ken"


def test_discord_without_a_token_is_refused() -> None:
    with pytest.raises(MessengerAuthError):
        oauth(FakeDiscord({"error": "invalid_grant"})).identity("c0de")
