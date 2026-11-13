from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.core.models import Cleanup
from apps.servers.models import Server
from apps.servers.services.monitor import ServerMonitor
from apps.servers.services.thinning import HistoryThinner


def _monitor() -> ServerMonitor:
    return ServerMonitor(timedelta(days=settings.MONITOR_HISTORY_DAYS))


@shared_task(name="servers.monitor")
def monitor_servers() -> None:
    """A task per server: one that does not answer delays only its own reading."""
    for server_id in Server.objects.values_list("pk", flat=True):
        monitor_server.apply_async((server_id,), expires=settings.MONITOR_INTERVAL)
    _monitor().prune()


@shared_task(name="servers.monitor_server")
def monitor_server(server_id: int) -> None:
    server = Server.objects.select_related("latest_sample").filter(pk=server_id).first()
    if server is not None:
        _monitor().poll(server)


@shared_task(name="servers.thin_history")
def thin_history() -> int:
    thinner = HistoryThinner(timedelta(days=settings.MONITOR_DETAIL_DAYS), timezone.now())
    return Cleanup.record("servers.thin_history", thinner.thin_all())
