from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.core.models import Cleanup
from apps.relays.models import Relay
from apps.relays.services.deployment import sync_relays
from apps.relays.services.load import prune_load
from apps.relays.services.monitor import RelayMonitor, report_caught_up, report_lagging
from apps.servers.services.thinning import HistoryThinner


@shared_task(name="relays.sync")
def sync(servers: list[int] | None = None) -> None:
    """The relays on these servers after a change of their groups; every relay by the minute."""
    sync_relays(servers)


@shared_task(name="relays.monitor")
def monitor() -> None:
    """A task per relay: one that does not answer delays only its own reading."""
    installed = Relay.objects.filter(installed_at__isnull=False).values_list("pk", flat=True)
    for relay_id in installed:
        monitor_relay.apply_async((relay_id,), expires=settings.RELAY_MONITOR_INTERVAL)
    report_lagging(timedelta(seconds=settings.GROUP_LAG_ALERT))
    report_caught_up()
    prune_load(timezone.now() - timedelta(days=settings.MONITOR_HISTORY_DAYS))


@shared_task(name="relays.monitor_relay")
def monitor_relay(relay_id: int) -> None:
    relay = Relay.objects.select_related("server").filter(pk=relay_id).first()
    if relay is not None:
        RelayMonitor(timedelta(seconds=settings.RELAY_MONITOR_INTERVAL)).poll(relay)


@shared_task(name="relays.thin_history")
def thin_history() -> int:
    """Old load readings keep one per quarter of an hour, like the server readings."""
    thinner = HistoryThinner(timedelta(days=settings.MONITOR_DETAIL_DAYS), timezone.now())
    removed = sum(thinner.thin_series(relay.samples.all()) for relay in Relay.objects.all())
    return Cleanup.record("relays.thin_history", removed)
