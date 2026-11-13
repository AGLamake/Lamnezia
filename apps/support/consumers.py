"""WebSocket of the support pages: tells an open page that a thread or a counter changed."""

from typing import Any

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.accounts.models import User
from apps.support.services.subscriptions import follow

# Nobody is signed in, or the thread is not one the user may read: the page does not retry.
CLOSE_FORBIDDEN = 4403


class SupportConsumer(AsyncJsonWebsocketConsumer):
    """`ws/support/` follows the counters of the signed-in user, `ws/support/<pk>/` a thread too.

    The socket carries signals only (see services/live.py): the page fetches the change itself.
    The page's pings only keep the connection alive through proxies and get no answer.
    """

    _user: User | None = None
    _joined: tuple[str, ...] = ()

    async def connect(self) -> None:
        user = self.scope.get("user")
        pk = self.scope["url_route"]["kwargs"].get("pk")
        groups = await database_sync_to_async(follow)(user, pk) if isinstance(user, User) else None
        # Accepted before closing: a refused handshake reaches the page without the code.
        await self.accept()
        if groups is None:
            await self.close(code=CLOSE_FORBIDDEN)
            return
        self._user = user
        self._joined = tuple(groups)
        for group in groups:
            await self.channel_layer.group_add(group, self.channel_name)

    async def disconnect(self, code: int) -> None:
        for group in self._joined:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def support_signal(self, event: dict[str, Any]) -> None:
        if self._user is None or (event["staff_only"] and not self._user.is_staff_member):
            return
        await self.send_json({"signal": event["signal"]})
