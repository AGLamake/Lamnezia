"""The bots service: runs every messenger bot as the panel asks and reports how it goes.

Every BOT_SUPERVISOR_SECONDS it reads `BotRuntime` (turned on, restart asked, servers to
leave) and the bot token, starts, stops or restarts the bot to match, and writes the status
back. The panel never controls the container: it only changes `BotRuntime`.
"""

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.bots.adapters.base import BotAdapter
from apps.bots.models import BotRuntime, BotStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Wish:
    """What the panel wants of a bot right now."""

    token: str
    enabled: bool
    restart_requested_at: datetime | None
    leave: tuple[str, ...]

    @property
    def wanted(self) -> bool:
        return self.enabled and bool(self.token)


class BotSlot:
    """One bot of the service: its running task, the token it runs with and its last failure."""

    def __init__(self, adapter_class: type[BotAdapter], retry_after: timedelta) -> None:
        self._adapter_class = adapter_class
        self._retry_after = retry_after
        self._adapter: BotAdapter | None = None
        self._task: asyncio.Task[None] | None = None
        self._token = ""
        self._started_at: datetime | None = None
        self._failed_at: datetime | None = None
        self._error = ""

    @property
    def messenger(self) -> str:
        return self._adapter_class.messenger

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def tick(self) -> None:
        wish = await sync_to_async(self._read)()
        self._collect_failure()
        now = timezone.now()
        restart = self._restart_asked(wish)
        if self.running and (not wish.wanted or wish.token != self._token or restart):
            await self.stop()
        if not self.running and wish.wanted and (restart or self._may_retry(now)):
            self._start(wish.token, now)
        for server_id in wish.leave:
            await self._leave(server_id)
        await sync_to_async(self._report)(wish)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = self._adapter = None
        self._error, self._failed_at = "", None

    async def _leave(self, server_id: str) -> None:
        if self._adapter is None or not self.running:
            return
        try:
            await self._adapter.leave(server_id)
        except Exception:  # a messenger API error must not stop the other bots
            logger.exception("The %s bot could not leave server %s", self.messenger, server_id)

    def _start(self, token: str, now: datetime) -> None:
        self._adapter = self._adapter_class()
        self._task = asyncio.create_task(self._adapter.run(token))
        self._token, self._started_at = token, now
        self._error, self._failed_at = "", None

    def _restart_asked(self, wish: Wish) -> bool:
        asked = wish.restart_requested_at
        return asked is not None and (self._started_at is None or asked > self._started_at)

    def _may_retry(self, now: datetime) -> bool:
        return self._failed_at is None or now - self._failed_at >= self._retry_after

    def _collect_failure(self) -> None:
        """A bot task that ended on its own failed; keep why, without the token."""
        if self._task is None or not self._task.done():
            return
        error = None if self._task.cancelled() else self._task.exception()
        reason = f"{type(error).__name__}: {error}" if error else "The bot stopped by itself."
        self._error = reason.replace(self._token, "***") if self._token else reason
        self._failed_at = timezone.now()
        self._task = self._adapter = None
        logger.warning("The %s bot stopped: %s", self.messenger, self._error)

    def _read(self) -> Wish:
        close_old_connections()  # the service runs for months; the database may have restarted
        with transaction.atomic():
            pk = BotRuntime.of(self.messenger).pk
            runtime = BotRuntime.objects.select_for_update().get(pk=pk)
            leave = tuple(runtime.leave_requests)
            if leave:
                runtime.leave_requests = []
                runtime.save(update_fields=["leave_requests"])
        return Wish(
            self._adapter_class.token(), runtime.enabled, runtime.restart_requested_at, leave
        )

    def _report(self, wish: Wish) -> None:
        adapter = self._adapter if self.running else None
        BotRuntime.objects.filter(messenger=self.messenger).update(
            status=self._status(wish, adapter),
            identity=adapter.identity if adapter else "",
            error=self._error,
            servers=[asdict(server) for server in adapter.servers()] if adapter else [],
            started_at=self._started_at if adapter else None,
            seen_at=timezone.now(),
        )

    def _status(self, wish: Wish, adapter: BotAdapter | None) -> BotStatus:
        if not wish.token:
            return BotStatus.NO_TOKEN
        if not wish.enabled:
            return BotStatus.OFF
        if adapter is not None:
            return BotStatus.RUNNING if adapter.identity else BotStatus.STARTING
        return BotStatus.ERROR if self._error else BotStatus.STARTING


class Supervisor:
    def __init__(
        self,
        adapter_classes: list[type[BotAdapter]],
        *,
        interval: float | None = None,
        retry_after: timedelta | None = None,
    ) -> None:
        retry = retry_after or timedelta(seconds=settings.BOT_RETRY_SECONDS)
        self.slots = [BotSlot(adapter_class, retry) for adapter_class in adapter_classes]
        self._interval = settings.BOT_SUPERVISOR_SECONDS if interval is None else interval

    async def run(self) -> None:
        """Until the process stops; the bots stop with it."""
        try:
            while True:
                await self.tick()
                await asyncio.sleep(self._interval)
        finally:
            await asyncio.gather(*(slot.stop() for slot in self.slots))

    async def tick(self) -> None:
        for slot in self.slots:
            await slot.tick()
