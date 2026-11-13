import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import ClassVar

import pytest
import pytest_asyncio
from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.accounts.models import Messenger
from apps.bots.adapters.base import BotAdapter, BotServer
from apps.bots.adapters.discord import DiscordAdapter
from apps.bots.adapters.telegram import TelegramAdapter
from apps.bots.models import BotRuntime, BotStatus
from apps.bots.services.control import BotControl
from apps.bots.services.supervisor import Supervisor
from apps.core.models import SiteSettings
from apps.core.services.site_settings import cached_settings

# The service reads the database from a worker thread: it must see committed rows.
pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]

TOKEN = "123:secret"


class ScriptedBot(BotAdapter):
    """A bot that connects at once (or fails as told) and then waits to be cancelled."""

    messenger = Messenger.DISCORD
    token_value = TOKEN
    failure: Exception | None = None
    runs: ClassVar[list[str]] = []
    left: ClassVar[list[str]] = []

    @classmethod
    def token(cls) -> str:
        return cls.token_value

    async def run(self, token: str) -> None:
        self.runs.append(token)
        if self.failure is not None:
            raise self.failure
        self.identity = "@lamnezia_bot"
        await asyncio.Event().wait()

    def servers(self) -> list[BotServer]:
        return [BotServer("42", "Friends", 7)]

    async def leave(self, server_id: str) -> None:
        self.left.append(server_id)


@pytest.fixture
def bot() -> Iterator[type[ScriptedBot]]:
    class Bot(ScriptedBot):
        runs: ClassVar[list[str]] = []
        left: ClassVar[list[str]] = []

    yield Bot


@pytest_asyncio.fixture
async def service(bot: type[ScriptedBot]) -> AsyncIterator[Supervisor]:
    supervisor = Supervisor([bot], interval=0, retry_after=timedelta(hours=1))
    yield supervisor
    await asyncio.gather(*(slot.stop() for slot in supervisor.slots))


async def tick(service: Supervisor) -> BotRuntime:
    """One round of the service, plus a moment for the bot task to connect, then its report."""
    await service.tick()
    await asyncio.sleep(0)
    await service.tick()
    return await BotRuntime.objects.aget(messenger=Messenger.DISCORD)


async def change(**fields: object) -> None:
    await BotRuntime.objects.filter(messenger=Messenger.DISCORD).aupdate(**fields)


async def test_a_bot_with_a_token_starts_and_reports_itself(
    service: Supervisor, bot: type[ScriptedBot]
) -> None:
    runtime = await tick(service)

    assert (runtime.status, runtime.identity, runtime.error) == (
        BotStatus.RUNNING,
        "@lamnezia_bot",
        "",
    )
    assert runtime.servers == [{"id": "42", "name": "Friends", "members": 7}]
    assert runtime.started_at is not None
    assert runtime.is_responding(timezone.now())
    assert bot.runs == [TOKEN]


async def test_without_a_token_the_bot_does_not_start(
    service: Supervisor, bot: type[ScriptedBot]
) -> None:
    bot.token_value = ""

    runtime = await tick(service)

    assert (runtime.status, bot.runs) == (BotStatus.NO_TOKEN, [])


async def test_turning_off_stops_the_bot_and_on_starts_it_again(
    service: Supervisor, bot: type[ScriptedBot]
) -> None:
    await tick(service)

    await change(enabled=False)
    runtime = await tick(service)
    assert (runtime.status, runtime.identity, runtime.servers) == (BotStatus.OFF, "", [])
    assert not service.slots[0].running

    await change(enabled=True)
    runtime = await tick(service)
    assert runtime.status == BotStatus.RUNNING
    assert bot.runs == [TOKEN, TOKEN]


async def test_a_restart_request_restarts_the_bot_once(
    service: Supervisor, bot: type[ScriptedBot]
) -> None:
    await tick(service)

    await change(restart_requested_at=timezone.now())
    await tick(service)
    await tick(service)

    assert bot.runs == [TOKEN, TOKEN]


async def test_a_new_token_restarts_the_bot_with_it(
    service: Supervisor, bot: type[ScriptedBot]
) -> None:
    await tick(service)

    bot.token_value = "456:other"
    await tick(service)

    assert bot.runs == [TOKEN, "456:other"]


async def test_a_failure_is_shown_without_the_token_and_retried_later(
    service: Supervisor, bot: type[ScriptedBot]
) -> None:
    bot.failure = RuntimeError(f"Unauthorized token {TOKEN}")

    runtime = await tick(service)

    assert runtime.status == BotStatus.ERROR
    assert runtime.error == "RuntimeError: Unauthorized token ***"
    assert bot.runs == [TOKEN]  # retried only after retry_after

    bot.failure = None
    await change(restart_requested_at=timezone.now())
    runtime = await tick(service)
    assert (runtime.status, runtime.error) == (BotStatus.RUNNING, "")


async def test_the_bot_leaves_a_server_the_administrator_asked_it_to(
    service: Supervisor, bot: type[ScriptedBot]
) -> None:
    runtime = await tick(service)

    await sync_to_async(BotControl().leave)(runtime, "42")
    runtime = await tick(service)

    assert bot.left == ["42"]
    assert runtime.leave_requests == []


async def test_the_run_loop_stops_its_bots_when_cancelled(bot: type[ScriptedBot]) -> None:
    supervisor = Supervisor([bot], interval=0.01)
    task = asyncio.create_task(supervisor.run())
    async with asyncio.timeout(5):
        while not supervisor.slots[0].running:
            await asyncio.sleep(0.01)

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert not supervisor.slots[0].running


@pytest.mark.parametrize(
    ("adapter", "field"),
    [(TelegramAdapter, "telegram_bot_token"), (DiscordAdapter, "discord_bot_token")],
)
async def test_a_bot_reads_a_new_token_even_without_a_shared_cache(
    adapter: type[BotAdapter], field: str
) -> None:
    """In dev the panel and the bots service have caches of their own: the panel's «settings
    changed» never reaches the service, and the token is read from the database anyway.
    """
    await sync_to_async(cached_settings.get)()  # this process holds the old settings
    await SiteSettings.objects.filter(pk=1).aupdate(**{field: "456:new"})  # saved elsewhere

    assert await sync_to_async(adapter.token)() == "456:new"
