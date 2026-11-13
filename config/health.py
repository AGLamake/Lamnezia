"""Liveness probes used by docker healthchecks and to check the proxy chain."""

from channels.generic.websocket import AsyncWebsocketConsumer
from django.db import DatabaseError, connection
from django.http import HttpRequest, JsonResponse


def healthz(request: HttpRequest) -> JsonResponse:
    """Report 200 when the web process can reach the database, 503 otherwise."""
    try:
        connection.ensure_connection()
    except DatabaseError:
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


class PingConsumer(AsyncWebsocketConsumer):
    """Answers every message with "pong": proves WebSocket works through Caddy."""

    async def receive(self, text_data: str | None = None, bytes_data: bytes | None = None) -> None:
        await self.send(text_data="pong")
