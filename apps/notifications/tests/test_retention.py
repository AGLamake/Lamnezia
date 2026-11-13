from datetime import timedelta

import pytest
from django.utils import timezone

from apps.notifications.models import Delivery, DeliveryStatus, Notification
from apps.notifications.services.retention import (
    INTERRUPTED_ERROR,
    StuckDeliveries,
    prune_deliveries,
)

pytestmark = pytest.mark.django_db

AGO = timedelta(hours=1)


def delivery(
    status: DeliveryStatus, *, age: timedelta, attempts: int = 0, key: str | None = None
) -> Delivery:
    notification = Notification.objects.create(event="server_down", key=key)
    moment = timezone.now() - age
    row = Delivery.objects.create(
        notification=notification,
        channel_name="hook",
        status=status,
        attempts=attempts,
        claimed_at=moment if status == DeliveryStatus.SENDING else None,
    )
    Delivery.objects.filter(pk=row.pk).update(created_at=moment)
    Notification.objects.filter(pk=notification.pk).update(created_at=moment)
    row.refresh_from_db()
    return row


def test_a_stuck_delivery_goes_back_to_the_queue_with_the_attempt_counted() -> None:
    stuck = delivery(DeliveryStatus.SENDING, age=AGO, attempts=1)

    assert StuckDeliveries(max_attempts=5).recover(timezone.now() - AGO / 2) == [stuck.pk]

    stuck.refresh_from_db()
    assert (stuck.status, stuck.attempts, stuck.error) == (
        DeliveryStatus.PENDING,
        2,
        INTERRUPTED_ERROR,
    )


def test_a_stuck_delivery_without_attempts_left_fails() -> None:
    stuck = delivery(DeliveryStatus.SENDING, age=AGO, attempts=4)

    assert StuckDeliveries(max_attempts=5).recover(timezone.now() - AGO / 2) == []

    stuck.refresh_from_db()
    assert (stuck.status, stuck.attempts) == (DeliveryStatus.FAILED, 5)


def test_a_delivery_being_sent_now_is_left_alone() -> None:
    sending = delivery(DeliveryStatus.SENDING, age=timedelta(seconds=5))
    pending = delivery(DeliveryStatus.PENDING, age=AGO)

    assert StuckDeliveries(max_attempts=5).recover(timezone.now() - AGO / 2) == []

    sending.refresh_from_db()
    pending.refresh_from_db()
    assert (sending.status, pending.status) == (DeliveryStatus.SENDING, DeliveryStatus.PENDING)


def test_old_finished_deliveries_are_deleted_with_their_notifications() -> None:
    old = timedelta(days=100)
    delivery(DeliveryStatus.SENT, age=old)
    delivery(DeliveryStatus.FAILED, age=old)
    keyed = delivery(DeliveryStatus.SENT, age=old, key="server_down:1:never")
    waiting = delivery(DeliveryStatus.PENDING, age=old)
    recent = delivery(DeliveryStatus.SENT, age=timedelta(days=10))

    assert prune_deliveries(timezone.now() - timedelta(days=90)) == 3

    assert set(Delivery.objects.values_list("pk", flat=True)) == {waiting.pk, recent.pk}
    assert set(Notification.objects.values_list("pk", flat=True)) == {
        keyed.notification_id,
        waiting.notification_id,
        recent.notification_id,
    }
