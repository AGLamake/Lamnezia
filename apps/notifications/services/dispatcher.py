"""Sending one delivery, with retries on a growing delay."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from django.conf import settings
from django.urls import reverse
from django.utils import timezone, translation

from apps.accounts.models import Messenger
from apps.accounts.services.link_offer import letter_offer
from apps.bots.services.session import Panels
from apps.core.services.site_settings import cached_settings
from apps.notifications.events import EVENTS, Event
from apps.notifications.models import Delivery, DeliveryStatus, Notification
from apps.notifications.services.browser import push_to_user
from apps.notifications.services.channels import (
    DeliveryError,
    HttpClient,
    Message,
    build_channel,
)
from apps.notifications.services.personal import Destination, PersonalChannels

_ERROR_LIMIT = 300


class Dispatcher:
    """Sends one delivery per call. Attempts are counted in the row, not in the job queue."""

    def __init__(
        self,
        http: HttpClient,
        *,
        max_attempts: int,
        retry_delay: int,
        clock: Callable[[], datetime] = timezone.now,
    ) -> None:
        self._http = http
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay
        self._clock = clock

    @classmethod
    def from_settings(cls) -> "Dispatcher":
        return cls(
            HttpClient.from_settings(),
            max_attempts=settings.NOTIFICATION_MAX_ATTEMPTS,
            retry_delay=settings.NOTIFICATION_RETRY_DELAY,
        )

    def deliver(self, delivery_id: int) -> int | None:
        """Send the delivery; return seconds until the next attempt, or None when it is done."""
        # Taking the row from `pending` is atomic: two jobs for one delivery never both send.
        claimed = Delivery.objects.filter(pk=delivery_id, status=DeliveryStatus.PENDING).update(
            status=DeliveryStatus.SENDING, claimed_at=self._clock()
        )
        if not claimed:
            return None
        delivery = Delivery.objects.select_related(
            "notification", "notification__user", "channel"
        ).get(pk=delivery_id)
        delivery.attempts += 1
        try:
            # The bot's buttons are in the recipient's language, as the message is.
            with translation.override(language_of(delivery)):
                destination = self._send(delivery, message_of(delivery.notification))
        except DeliveryError as error:
            return self._failed(delivery, error)
        delivery.status, delivery.error, delivery.sent_at = DeliveryStatus.SENT, "", self._clock()
        delivery.save(update_fields=["status", "error", "sent_at", "attempts"])
        if destination is not None:
            self._reached(delivery.notification, destination)
        return None

    @staticmethod
    def _reached(notification: Notification, destination: Destination) -> None:
        """The message reached the user: the notification keeps by which channel."""
        notification.sent_via = destination.via
        notification.save(update_fields=["sent_via"])
        chat = destination.chat
        if chat is not None:
            # The message went under the bot's panel: the next panel opens below it.
            Panels().bury(Messenger(chat.messenger), chat.external_id)

    def _send(self, delivery: Delivery, message: Message) -> Destination | None:
        """Send the message; return the user's channel it went to, None for a staff channel
        and the user's browsers.
        """
        if delivery.to_browser:
            user = delivery.notification.user
            if user is None:
                raise DeliveryError("the user is deleted", retryable=False)
            push_to_user(user, message, self._http)
            return None
        if not delivery.to_user:
            if delivery.channel is None:
                raise DeliveryError("the channel is deleted", retryable=False)
            build_channel(delivery.channel, self._http).send(message)
            return None
        destinations = self._destinations(delivery.notification)
        if not destinations:
            raise DeliveryError("the user's other channels already have it", retryable=False)
        errors: list[DeliveryError] = []
        for destination in destinations:
            try:
                destination.channel.send(self._addressed(message, destination, delivery))
            except DeliveryError as error:
                errors.append(error)
            else:
                return destination
        raise DeliveryError(
            "; ".join(map(str, errors)), retryable=any(error.retryable for error in errors)
        )

    @staticmethod
    def _addressed(message: Message, destination: Destination, delivery: Delivery) -> Message:
        """The message for this channel: a letter to the user links to their notification
        settings, and one about the end of access also offers to link a messenger.
        """
        user = delivery.notification.user
        if destination.chat is not None or user is None:
            return message
        message = replace(message, settings_url=_settings_url())
        if not EVENTS[Event(message.event)].link_offer:
            return message
        offer = letter_offer(user, cached_settings.get())
        return replace(message, offer=offer) if offer else message

    def _destinations(self, notification: Notification) -> list[Destination]:
        """The messenger `via`, else the user's choice (DESIGN.md 6.8). A `fallback` event goes
        on to the user's other channels but `skip_via` until one takes it; any other falls back
        to email.
        """
        user = notification.user
        chosen = notification.via or (user.notify_via if user else "")
        channels = PersonalChannels(user, self._http)
        if EVENTS[Event(notification.event)].fallback:
            return channels.all(chosen, notification.skip_via)
        return [channels.first(chosen)]

    def _failed(self, delivery: Delivery, error: DeliveryError) -> int | None:
        retry = error.retryable and delivery.attempts < self._max_attempts
        delivery.status = DeliveryStatus.PENDING if retry else DeliveryStatus.FAILED
        delivery.error = str(error)[:_ERROR_LIMIT]
        delivery.save(update_fields=["status", "error", "attempts"])
        return self._retry_delay * 2 ** (delivery.attempts - 1) if retry else None


def language_of(delivery: Delivery) -> str:
    """The language of the recipient: the user's own for a delivery to them, else the site's."""
    user = delivery.notification.user
    if (delivery.to_user or delivery.to_browser) and user is not None:
        return user.notification_language
    return settings.LANGUAGE_CODE


def message_of(notification: Notification) -> Message:
    """The notification in the active language."""
    title, text = EVENTS[Event(notification.event)].render(notification.data)
    return Message(
        uid=str(notification.uid),
        event=notification.event,
        created_at=notification.created_at,
        title=title,
        text=text,
        data=notification.data,
    )


def _settings_url() -> str:
    """The notification settings in the user's profile, as an address for a letter."""
    return f"{str(settings.SITE_URL).rstrip('/')}{reverse('accounts:profile')}#notifications"
