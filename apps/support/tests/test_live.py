"""The support WebSocket: who may follow a thread and which signals reach whom."""

import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from channels.db import database_sync_to_async
from channels.testing.websocket import WebsocketCommunicator
from django.test import Client

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.support.consumers import CLOSE_FORBIDDEN
from apps.support.models import Conversation
from apps.support.services.chat import MyChat
from apps.support.services.inbox import Inbox
from apps.support.services.tickets import MyRequests
from apps.support.tests.test_inbox import request_of
from config.asgi import application

# The consumer reads the database from its own threads: the data must be committed.
pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

# A message must reach an open page in under a second.
DELIVERY_SECONDS = 1


def _session_cookie(user: User | None) -> list[tuple[bytes, bytes]]:
    headers = [(b"origin", b"http://localhost")]
    if user is not None:
        client = Client()
        client.force_login(user)
        headers.append((b"cookie", f"sessionid={client.cookies['sessionid'].value}".encode()))
    return headers


class Page:
    """An open page of `user` with the support socket at `path`."""

    def __init__(self, user: User | None, path: str) -> None:
        self.user = user
        self.path = path

    async def open(self) -> WebsocketCommunicator:
        headers = await database_sync_to_async(_session_cookie)(self.user)
        socket = WebsocketCommunicator(application, self.path, headers=headers)
        connected, _ = await socket.connect()
        assert connected
        return socket


async def signals(socket: WebsocketCommunicator) -> list[str]:
    """Every signal the page got by now."""
    received = []
    while not await socket.receive_nothing(timeout=0.1):
        received.append(json.loads(await socket.receive_from(DELIVERY_SECONDS))["signal"])
    return received


async def closed_code(socket: WebsocketCommunicator) -> int:
    output = await socket.receive_output(DELIVERY_SECONDS)
    assert output["type"] == "websocket.close"
    return int(output["code"])


@database_sync_to_async
def make_user(role: Role = Role.USER) -> User:
    return UserFactory(role=role)


@database_sync_to_async
def open_request(user: User) -> Conversation:
    return request_of(user)


@pytest_asyncio.fixture
async def user() -> AsyncIterator[User]:
    yield await make_user()


@pytest_asyncio.fixture
async def admin() -> AsyncIterator[User]:
    yield await make_user(Role.ADMIN)


async def test_a_user_cannot_follow_someone_elses_thread(user: User) -> None:
    other = await open_request(await make_user())

    socket = await Page(user, f"/ws/support/{other.pk}/").open()

    assert await closed_code(socket) == CLOSE_FORBIDDEN


async def test_a_moderator_follows_only_threads_of_their_users(admin: User, user: User) -> None:
    moderator = await make_user(Role.MODERATOR)
    conversation = await open_request(user)
    await database_sync_to_async(Inbox(admin).assign)(conversation, moderator)

    socket = await Page(moderator, f"/ws/support/{conversation.pk}/").open()

    assert await closed_code(socket) == CLOSE_FORBIDDEN
    user.manager = moderator
    await user.asave(update_fields=["manager"])
    socket = await Page(moderator, f"/ws/support/{conversation.pk}/").open()
    assert await socket.receive_nothing()
    await socket.disconnect()


async def test_nobody_signed_in_is_refused() -> None:
    socket = await Page(None, "/ws/support/").open()

    assert await closed_code(socket) == CLOSE_FORBIDDEN


async def test_a_message_of_the_user_reaches_the_admin_at_once(admin: User, user: User) -> None:
    conversation = await open_request(user)
    inbox = await Page(admin, "/ws/support/").open()
    thread = await Page(admin, f"/ws/support/{conversation.pk}/").open()

    await database_sync_to_async(MyChat(user).send)("Hello?")
    await database_sync_to_async(MyRequests(user).reply)(conversation, "Any news?")

    assert await signals(inbox) == ["unread", "unread"]
    assert await signals(thread) == ["unread", "thread", "unread"]
    await inbox.disconnect()
    await thread.disconnect()


async def test_the_user_hears_answers_and_read_marks_but_no_notes(admin: User, user: User) -> None:
    conversation = await open_request(user)
    page = await Page(user, f"/ws/support/{conversation.pk}/").open()
    staff = Inbox(admin)

    await database_sync_to_async(staff.note)(conversation, "Looks like the provider")
    assert await signals(page) == []
    await database_sync_to_async(staff.mark_read)(conversation)
    assert await signals(page) == ["thread"]
    await database_sync_to_async(staff.reply)(conversation, "Fixed")
    # The last one: the line about the answer in the user's chat.
    assert await signals(page) == ["thread", "unread", "thread", "thread"]
    await page.disconnect()


async def test_any_page_of_the_user_hears_the_chat(admin: User, user: User) -> None:
    await database_sync_to_async(MyChat(user).send)("Hello?")
    chat = await database_sync_to_async(MyChat(user).get)()
    assert chat is not None
    page = await Page(user, "/ws/support/").open()

    await database_sync_to_async(Inbox(admin).reply)(chat, "Hi!")

    assert await signals(page) == ["thread", "unread"]
    await page.disconnect()


async def test_an_answer_reaches_the_lists_of_the_other_staff(admin: User, user: User) -> None:
    conversation = await open_request(user)
    colleague = await Page(await make_user(Role.ADMIN), "/ws/support/").open()

    await database_sync_to_async(Inbox(admin).note)(conversation, "Looks like the provider")
    assert await signals(colleague) == []
    await database_sync_to_async(Inbox(admin).reply)(conversation, "Fixed")

    assert "inbox" in await signals(colleague)
    await colleague.disconnect()


async def test_a_ping_keeps_the_socket_open_and_gets_no_answer(user: User) -> None:
    socket = await Page(user, "/ws/support/").open()

    await socket.send_json_to({"type": "ping"})

    assert await socket.receive_nothing()
    await socket.disconnect()
