"""Housekeeping of the delivery log: deliveries stuck after a worker crash, old records."""

from datetime import datetime

from django.db.models import F, Q

from apps.notifications.models import Delivery, DeliveryStatus, Notification

INTERRUPTED_ERROR = "the worker stopped while sending"


class StuckDeliveries:
    """Deliveries left in "sending" by a worker that died: the attempt counts as a failed one."""

    def __init__(self, *, max_attempts: int) -> None:
        self._max_attempts = max_attempts

    def recover(self, taken_before: datetime) -> list[int]:
        """Put them back in the queue or fail them; return the ids to send again."""
        stuck = Delivery.objects.filter(status=DeliveryStatus.SENDING).filter(
            Q(claimed_at__lt=taken_before) | Q(claimed_at__isnull=True, created_at__lt=taken_before)
        )
        resend = []
        for delivery in stuck:
            if self._release(delivery):
                resend.append(delivery.pk)
        return resend

    def _release(self, delivery: Delivery) -> bool:
        """True when it goes back to the queue. A worker that answers late keeps its result."""
        retry = delivery.attempts + 1 < self._max_attempts
        released = Delivery.objects.filter(
            pk=delivery.pk, status=DeliveryStatus.SENDING, claimed_at=delivery.claimed_at
        ).update(
            status=DeliveryStatus.PENDING if retry else DeliveryStatus.FAILED,
            attempts=F("attempts") + 1,
            error=INTERRUPTED_ERROR,
        )
        return bool(released) and retry


def prune_deliveries(before: datetime) -> int:
    """Delete sent and failed deliveries made before `before`; return how many.

    Notifications left without deliveries go too, except those with a `key`: the key keeps
    a repeated event from being sent again.
    """
    _, deleted = Delivery.objects.filter(
        status__in=(DeliveryStatus.SENT, DeliveryStatus.FAILED), created_at__lt=before
    ).delete()
    Notification.objects.filter(
        key__isnull=True, created_at__lt=before, deliveries__isnull=True
    ).delete()
    return deleted.get(Delivery._meta.label, 0)
