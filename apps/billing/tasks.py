"""Background tasks of billing: orders left unpaid are marked expired."""

from celery import shared_task
from django.utils import timezone

from apps.billing.services.orders import expire


@shared_task(name="billing.expire_orders")
def expire_orders() -> int:
    return expire(timezone.now())
