"""Routing and delivery of notifications (DESIGN.md 6.7).

Any part of the panel calls `emit(event, data)`; the event goes to every enabled channel routed for
it and, for user events, to the user's email. Each channel gets its own delivery row and job, so a
failing channel neither blocks nor repeats the others.

Occurrences of a joined event (chat messages) about one user, close in time, make one
notification: `EventSeries`.
"""

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User

# The module, not the task: the tasks module imports the reminders, which call `emit`.
from apps.notifications import tasks
from apps.notifications.events import EVENTS, Event
from apps.notifications.models import Channel, Delivery, DeliveryStatus, Notification, Route


def emit(  # noqa: PLR0913 - the routing of one event is keyword-only
    event: Event,
    data: Mapping[str, Any] | None = None,
    *,
    key: str | None = None,
    user: User | None = None,
    via: str = "",
    skip_via: str = "",
) -> Notification | None:
    """Queue `event` after the current transaction commits.

    `key` names the occurrence: a second emit with the same key is ignored. `user` is who the event
    is about; `via` is the messenger they hear of it in instead of their own choice (falling back
    to email, as the choice does); `skip_via` is a channel of theirs that already has the news.
    Returns None when the key was seen or nobody is to hear of it.
    """
    channels = list(Channel.objects.filter(enabled=True, routes__event=event))
    kind = EVENTS[event]
    to_user = kind.to_user and user is not None and bool(user.email or via)
    if not channels and not to_user:
        return None
    with transaction.atomic():
        if kind.joined and user is not None:
            joined = EventSeries(event, user).join(dict(data or {}))
            if joined is not None:
                return joined
        notification = _record(
            event, dict(data or {}), key, user, {"via": via, "skip_via": skip_via}
        )
        if notification is None:
            return None
        deliveries = [_to_channel(notification, channel) for channel in channels]
        if to_user:
            deliveries.append(Delivery(notification=notification, to_user=True, channel_name=""))
        wait = settings.NOTIFICATION_JOIN_WINDOW if kind.joined else None
        _queue(Delivery.objects.bulk_create(deliveries), wait)
    return notification


def send_test(channel: Channel) -> Delivery:
    """A test message to one channel, even a disabled one; the result shows in the log."""
    with transaction.atomic():
        notification = Notification.objects.create(event=Event.TEST, data={"channel": channel.name})
        delivery = _to_channel(notification, channel)
        delivery.save()
        _queue([delivery])
    return delivery


def set_routes(channel: Channel, events: list[str]) -> None:
    channel.routes.exclude(event__in=events).delete()
    Route.objects.bulk_create(
        [Route(channel=channel, event=event) for event in events], ignore_conflicts=True
    )


def _record(
    event: Event, data: dict[str, Any], key: str | None, user: User | None, routing: dict[str, str]
) -> Notification | None:
    fields = {"event": event, "data": data, "user": user, **routing}
    if key is None:
        return Notification.objects.create(**fields)
    notification, created = Notification.objects.get_or_create(key=key, defaults=fields)
    return notification if created else None


def _to_channel(notification: Notification, channel: Channel) -> Delivery:
    return Delivery(notification=notification, channel=channel, channel_name=str(channel))


def _queue(deliveries: list[Delivery], wait: timedelta | None = None) -> None:
    """Send the deliveries after the commit; `wait`: not before this much time has passed."""
    ids = [delivery.pk for delivery in deliveries]
    countdown = int(wait.total_seconds()) if wait else 0

    def send() -> None:
        for pk in ids:
            tasks.deliver.apply_async((pk,), countdown=countdown)

    transaction.on_commit(send)


class EventSeries:
    """Occurrences of a joined event about one user, while its first one waits to be sent.

    The deliveries of the first one wait out `NOTIFICATION_JOIN_WINDOW`; the ones that come
    meanwhile add their `text` to it instead of making notifications of their own.
    """

    TEXT_LIMIT = 2000

    def __init__(self, event: Event, user: User) -> None:
        self._event = event
        self._user = user

    def join(self, data: dict[str, Any]) -> Notification | None:
        """The waiting notification with `data` added to it; None when none waits."""
        notification = self._waiting()
        if notification is None:
            return None
        texts = [str(notification.data.get("text", "")), str(data.get("text", ""))]
        notification.data = {
            **data,
            "text": "\n".join(texts)[: self.TEXT_LIMIT],
            "count": int(notification.data.get("count", 1)) + 1,
        }
        notification.save(update_fields=["data"])
        return notification

    def _waiting(self) -> Notification | None:
        since = timezone.now() - settings.NOTIFICATION_JOIN_WINDOW
        notification = (
            Notification.objects.select_for_update()
            .filter(event=self._event, user=self._user, created_at__gte=since)
            .order_by("-created_at")
            .first()
        )
        if notification is None:
            return None
        # Locking the deliveries holds off a worker about to take one: it reads the joined text.
        deliveries = list(Delivery.objects.select_for_update().filter(notification=notification))
        untouched = all(d.status == DeliveryStatus.PENDING and not d.attempts for d in deliveries)
        return notification if deliveries and untouched else None
