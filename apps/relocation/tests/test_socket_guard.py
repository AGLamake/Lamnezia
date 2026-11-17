"""The WebSockets of a frozen panel: no consumer opens or reads a socket while the panel moves."""

import pytest
from channels.db import database_sync_to_async
from channels.testing.websocket import WebsocketCommunicator
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.relocation.models import MoveState, PanelMove
from apps.relocation.socket_guard import CLOSE_MOVING
from apps.relocation.tests.helpers import make_move
from config.asgi import application

# The consumer reads the database from its own threads: the data must be committed.
pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

TIMEOUT = 1


@database_sync_to_async
def signed_in_headers() -> list[tuple[bytes, bytes]]:
    user: User = UserFactory()
    client = Client()
    client.force_login(user)
    cookie = f"sessionid={client.cookies['sessionid'].value}".encode()
    return [(b"origin", b"http://localhost"), (b"cookie", cookie)]


@database_sync_to_async
def freeze(state: MoveState = MoveState.PREPARING) -> PanelMove:
    return make_move(state)


async def support_page() -> WebsocketCommunicator:
    return WebsocketCommunicator(application, "/ws/support/", headers=await signed_in_headers())


async def test_a_frozen_panel_refuses_the_socket() -> None:
    socket = await support_page()
    await freeze()

    connected, code = await socket.connect(TIMEOUT)

    assert not connected
    assert code == CLOSE_MOVING


async def test_a_message_on_an_open_socket_closes_it_unread() -> None:
    socket = await support_page()
    connected, _ = await socket.connect(TIMEOUT)
    assert connected
    await freeze()

    await socket.send_json_to({"type": "ping"})

    output = await socket.receive_output(TIMEOUT)
    assert output == {"type": "websocket.close", "code": CLOSE_MOVING}


async def test_a_finished_move_lets_the_socket_open() -> None:
    socket = await support_page()
    await freeze(MoveState.ARRIVED)

    connected, _ = await socket.connect(TIMEOUT)
    await socket.disconnect()

    assert connected


async def test_ping_answers_on_a_frozen_panel() -> None:
    await freeze()
    socket = WebsocketCommunicator(
        application, "/ws/ping", headers=[(b"origin", b"http://localhost")]
    )

    connected, _ = await socket.connect(TIMEOUT)
    await socket.send_to(text_data="ping")
    reply = await socket.receive_from(TIMEOUT)
    await socket.disconnect()

    assert connected
    assert reply == "pong"
