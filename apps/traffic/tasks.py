from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.core.models import Cleanup
from apps.traffic.services.limits import LimitEnforcer
from apps.traffic.services.poller import TrafficPoller
from apps.traffic.services.rollup import TrafficRollup
from apps.traffic.services.speed import SpeedLimiter
from apps.traffic.services.targets import poll_targets


@shared_task(name="traffic.collect")
def collect_traffic() -> None:
    """A task per server: one that does not answer delays only its own reading."""
    for target in poll_targets:
        collect_server.apply_async((target.name,), expires=settings.TRAFFIC_INTERVAL)


@shared_task(name="traffic.collect_server")
def collect_server(name: str) -> None:
    target = poll_targets.get(name)
    if target is not None:
        TrafficPoller().poll_all([target])


@shared_task(name="traffic.enforce_limits")
def enforce_limits() -> None:
    LimitEnforcer(timezone.now()).enforce_all()


@shared_task(name="traffic.apply_speed_limits")
def apply_speed_limits() -> None:
    SpeedLimiter().sync_all()


@shared_task(name="traffic.roll_up")
def roll_up() -> int:
    folded = TrafficRollup(timedelta(days=settings.TRAFFIC_HOURLY_DAYS), timezone.now()).run()
    return Cleanup.record("traffic.roll_up", folded)
