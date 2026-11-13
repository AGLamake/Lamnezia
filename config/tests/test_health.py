from unittest import mock

import pytest
from channels.testing.websocket import WebsocketCommunicator
from django.db import OperationalError
from django.test import Client

from config.asgi import application


@pytest.mark.django_db
def test_healthz_reports_ok_when_database_is_reachable(client: Client) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_reports_503_when_database_is_down(client: Client) -> None:
    with mock.patch("config.health.connection.ensure_connection", side_effect=OperationalError):
        response = client.get("/healthz")

    assert response.status_code == 503


# Channels checks the thread's database connections before each event: after the async tests
# with a database, that thread holds one, so the database must be allowed here too.
@pytest.mark.django_db
@pytest.mark.asyncio
async def test_ws_ping_answers_pong() -> None:
    communicator = WebsocketCommunicator(
        application, "/ws/ping", headers=[(b"origin", b"http://localhost")]
    )

    connected, _ = await communicator.connect()
    await communicator.send_to(text_data="ping")
    reply = await communicator.receive_from()
    await communicator.disconnect()

    assert connected
    assert reply == "pong"


@pytest.mark.asyncio
async def test_ws_ping_rejects_foreign_origin() -> None:
    communicator = WebsocketCommunicator(
        application, "/ws/ping", headers=[(b"origin", b"https://evil.example.org")]
    )

    connected, _ = await communicator.connect()

    assert not connected
