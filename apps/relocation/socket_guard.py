"""Closes the panel's WebSockets while it moves to another machine, as the 503 does for changes."""

from typing import Any

from channels.db import database_sync_to_async

from apps.relocation.services.freeze import panel_frozen

# Only echoes and proves the proxy chain works: the copy on the new machine is checked with it.
ALLOWED_PATHS = frozenset({"/ws/ping"})
# Events a consumer acts on; `websocket.disconnect` still reaches it, so it leaves its groups.
GUARDED_EVENTS = frozenset({"websocket.connect", "websocket.receive"})
# The panel is moving: the page falls back to reading and connects again later.
CLOSE_MOVING = 4503

Scope = dict[str, Any]
Message = dict[str, Any]


class FrozenSocketGuard:
    """ASGI middleware of the WebSocket routes: no consumer opens or reads a socket while frozen.

    A handshake is refused before the consumer sees it. A message on a socket opened before the
    freeze closes that socket instead of reaching the consumer, so nothing it carries can change
    the database the move has already copied.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> Any:
        if scope["type"] != "websocket" or scope["path"] in ALLOWED_PATHS:
            return await self.inner(scope, receive, send)

        async def guarded_receive() -> Message:
            while True:
                message: Message = await receive()
                if message["type"] not in GUARDED_EVENTS or not await _frozen():
                    return message
                await send({"type": "websocket.close", "code": CLOSE_MOVING})

        return await self.inner(scope, guarded_receive, send)


_frozen = database_sync_to_async(panel_frozen)
