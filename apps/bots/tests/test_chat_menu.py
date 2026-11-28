"""The Telegram menu of one chat follows its user's role and link without a bot restart."""

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import pytest

from apps.accounts.models import LinkedAccount, Messenger, Role, User
from apps.accounts.services.linking import AccountLinks
from apps.accounts.tests.factories import UserFactory
from apps.bots.services.chat_menu import ChatMenu
from apps.core.services.outbound import TelegramApi
from apps.notifications.services.channels import DeliveryError
from apps.notifications.tests.conftest import FakeHttp

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]
TOKEN = "123:secret"
API = TelegramApi()
SET, DELETE = API.method_url(TOKEN, "setMyCommands"), API.method_url(TOKEN, "deleteMyCommands")


def linked(role: str, chat_id: str = "1001") -> User:
    user = UserFactory(role=role)
    LinkedAccount.objects.create(user=user, messenger=Messenger.TELEGRAM, external_id=chat_id)
    return user


def sent(http: FakeHttp) -> list[tuple[str, dict[str, Any]]]:
    return [(post.url, json.loads(post.body)) for post in http.posts]


def test_a_staff_chat_gets_the_staff_commands() -> None:
    linked(Role.MODERATOR)
    http = FakeHttp()

    ChatMenu(TOKEN, http, API).refresh("1001")  # type: ignore[arg-type]

    [(url, body)] = sent(http)
    assert url == SET
    assert body["scope"] == {"type": "chat", "chat_id": "1001"}
    assert {"command": "admin", "description": "Administration"} in body["commands"]


@pytest.mark.parametrize("role", [Role.USER, None])
def test_a_chat_without_a_staff_member_falls_back_to_the_common_menu(role: str | None) -> None:
    if role:
        linked(role)
    http = FakeHttp()

    ChatMenu(TOKEN, http, API).refresh("1001")  # type: ignore[arg-type]

    assert sent(http) == [(DELETE, {"scope": {"type": "chat", "chat_id": "1001"}})]


def test_a_chat_that_blocked_the_bot_is_passed_over_and_a_server_error_retried() -> None:
    linked(Role.ADMIN)
    blocked = FakeHttp(failing={SET: DeliveryError("HTTP 403", retryable=False, status=403)})
    down = FakeHttp(failing={SET: DeliveryError("HTTP 502", retryable=True, status=502)})

    ChatMenu(TOKEN, blocked, API).refresh("1001")  # type: ignore[arg-type]
    with pytest.raises(DeliveryError):
        ChatMenu(TOKEN, down, API).refresh("1001")  # type: ignore[arg-type]


def test_nothing_is_sent_without_a_bot_token() -> None:
    linked(Role.ADMIN)
    http = FakeHttp()

    ChatMenu("", http, API).refresh("1001")  # type: ignore[arg-type]

    assert http.posts == []


@pytest.fixture
def refreshed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    chats: list[str] = []
    monkeypatch.setattr("apps.bots.tasks.ChatMenu.refresh", lambda _self, chat: chats.append(chat))
    return chats


def test_a_role_change_refreshes_the_menu_of_the_user_chat(
    refreshed: list[str], django_capture_on_commit_callbacks: OnCommit
) -> None:
    user = linked(Role.USER)

    with django_capture_on_commit_callbacks(execute=True):
        user.role = Role.MODERATOR
        user.save(update_fields=["role"])
    with django_capture_on_commit_callbacks(execute=True):
        user.role = Role.ADMIN
        user.save(update_fields=["role"])
        user.save(update_fields=["last_login"])
        user.save()

    assert refreshed == ["1001"]


def test_linking_and_unlinking_telegram_refresh_the_menu_of_a_staff_member(
    refreshed: list[str], django_capture_on_commit_callbacks: OnCommit
) -> None:
    user = UserFactory(role=Role.ADMIN)
    links = AccountLinks()

    with django_capture_on_commit_callbacks(execute=True):
        links.link(user, Messenger.TELEGRAM, "2002")
    with django_capture_on_commit_callbacks(execute=True):
        links.unlink(user, Messenger.TELEGRAM)
    with django_capture_on_commit_callbacks(execute=True):
        links.link(user, Messenger.DISCORD, "3003")
        links.link(UserFactory(role=Role.USER), Messenger.TELEGRAM, "4004")

    assert refreshed == ["2002", "2002"]


def test_a_link_moved_to_another_chat_takes_the_staff_menu_off_the_old_one(
    refreshed: list[str], django_capture_on_commit_callbacks: OnCommit
) -> None:
    user = linked(Role.MODERATOR, "1001")

    with django_capture_on_commit_callbacks(execute=True):
        AccountLinks().link(user, Messenger.TELEGRAM, "5005")

    assert refreshed == ["5005", "1001"]
