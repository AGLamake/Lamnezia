from datetime import timedelta

from celery import Task, shared_task
from django.conf import settings
from django.utils import timezone

from apps.core.models import Cleanup
from apps.notifications.services import reminders
from apps.notifications.services.dispatcher import Dispatcher
from apps.notifications.services.retention import StuckDeliveries, prune_deliveries


# No Celery limit on retries: the dispatcher counts attempts and gives up by itself.
@shared_task(bind=True, name="notifications.deliver", max_retries=None)
def deliver(self: "Task[[int], None]", delivery_id: int) -> None:
    delay = Dispatcher.from_settings().deliver(delivery_id)
    if delay is not None:
        raise self.retry(countdown=delay)


@shared_task(name="notifications.warn_expiring")
def warn_expiring() -> None:
    reminders.warn_expiring(timezone.now(), settings.ACCESS_EXPIRY_NOTICE)


@shared_task(name="notifications.recover_stuck")
def recover_stuck() -> None:
    stuck = StuckDeliveries(max_attempts=settings.NOTIFICATION_MAX_ATTEMPTS)
    for delivery_id in stuck.recover(timezone.now() - settings.NOTIFICATION_STUCK_AFTER):
        deliver.delay(delivery_id)


@shared_task(name="notifications.prune")
def prune() -> int:
    before = timezone.now() - timedelta(days=settings.NOTIFICATION_RETENTION_DAYS)
    return Cleanup.record("notifications.prune", prune_deliveries(before))
