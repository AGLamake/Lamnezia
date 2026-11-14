from datetime import timedelta

from celery import shared_task
from django.conf import settings

from apps.relays.models import Relay
from apps.relays.services.deployment import sync_relays
from apps.relays.services.monitor import RelayMonitor, report_lagging


@shared_task(name="relays.sync")
def sync() -> None:
    sync_relays()


@shared_task(name="relays.monitor")
def monitor() -> None:
    """A task per relay: one that does not answer delays only its own reading."""
    installed = Relay.objects.filter(installed_at__isnull=False).values_list("pk", flat=True)
    for relay_id in installed:
        monitor_relay.apply_async((relay_id,), expires=settings.RELAY_MONITOR_INTERVAL)
    report_lagging(timedelta(seconds=settings.GROUP_LAG_ALERT))


@shared_task(name="relays.monitor_relay")
def monitor_relay(relay_id: int) -> None:
    relay = Relay.objects.select_related("server").filter(pk=relay_id).first()
    if relay is not None:
        RelayMonitor(timedelta(seconds=settings.RELAY_MONITOR_INTERVAL)).poll(relay)
