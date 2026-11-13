"""What the panel shows about the bots and the requests it hands to the bots service."""

from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.accounts.models import LinkedAccount, Messenger
from apps.bots.models import BotRuntime, BotStatus


@dataclass(frozen=True)
class BotCard:
    """One messenger in the Bots block. `runtime` is None until the bots service reports it:
    the service has never run, or it has no bot for this messenger yet.
    """

    messenger: Messenger
    runtime: BotRuntime | None
    responding: bool
    linked_users: int

    @property
    def badge(self) -> str:
        if self.runtime is None:
            return "badge-muted"
        if not self.responding or self.runtime.status == BotStatus.ERROR:
            return "badge-danger"
        return "badge-success" if self.runtime.status == BotStatus.RUNNING else "badge-muted"


class BotControl:
    def cards(self, now: datetime | None = None) -> list[BotCard]:
        now = now or timezone.now()
        runtimes = {r.messenger: r for r in BotRuntime.objects.all()}
        linked = dict(
            LinkedAccount.objects.values_list("messenger").annotate(n=Count("pk")).order_by()
        )
        return [
            BotCard(
                messenger,
                runtimes.get(messenger),
                runtimes[messenger].is_responding(now) if messenger in runtimes else False,
                linked.get(messenger, 0),
            )
            for messenger in Messenger
        ]

    def turn(self, runtime: BotRuntime, *, on: bool) -> None:
        runtime.enabled = on
        runtime.save(update_fields=["enabled"])

    def restart(self, runtime: BotRuntime) -> None:
        runtime.restart_requested_at = timezone.now()
        runtime.save(update_fields=["restart_requested_at"])

    def leave(self, runtime: BotRuntime, server_id: str) -> None:
        """Ask the bot to leave a server it reported; the service takes the request on its tick."""
        with transaction.atomic():
            locked = BotRuntime.objects.select_for_update().get(pk=runtime.pk)
            if server_id not in locked.leave_requests:
                locked.leave_requests = [*locked.leave_requests, server_id]
                locked.save(update_fields=["leave_requests"])
