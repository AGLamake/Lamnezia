"""Notifications in the browser and the installed app (Web Push): the panel's key pair, the
subscriptions of the users and sending to them.

A user gets in their browsers the same events about them as by email and the bots; the staff get
the routed events through a `browser` channel, each within their zone (`audience.py`). A push
service that answers 404 or 410 has dropped the subscription, so the panel deletes it.
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Self
from urllib.parse import urlsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services.sessions import device_name
from apps.notifications.events import Event
from apps.notifications.models import Channel, ChannelKind, PushSubscription, VapidKey
from apps.notifications.services.addresses import AddressGuard, BlockedAddressError
from apps.notifications.services.audience import StaffAudience
from apps.notifications.services.channels import (
    DeliveryError,
    HttpClient,
    Message,
    NotificationChannel,
    register,
)
from apps.notifications.services.webpush import BrowserKeys, VapidKeys, public_key, unb64url

# The push service keeps an undelivered message this long while the device is offline.
_TIME_TO_LIVE = 24 * 60 * 60
# The push service has dropped the subscription.
_GONE = frozenset({404, 410})
_TITLE_LIMIT = 150
_BODY_LIMIT = 1000
_KEY_ROW = 1


class PanelKeys:
    """The panel's VAPID key pair, made once on first use and kept with the private key
    encrypted.
    """

    @staticmethod
    def get() -> VapidKeys:
        row = VapidKey.objects.filter(pk=_KEY_ROW).first()
        if row is None:
            row = PanelKeys._create()
        return VapidKeys.load(row.private_key)

    @staticmethod
    def public() -> str:
        return PanelKeys.get().public_text()

    @staticmethod
    def _create() -> VapidKey:
        """A fixed primary key: of two processes making the pair at once, one wins."""
        keys = VapidKeys.generate()
        try:
            with transaction.atomic():
                return VapidKey.objects.create(
                    pk=_KEY_ROW, public_key=keys.public_text(), private_key=keys.private_text()
                )
        except IntegrityError:
            return VapidKey.objects.get(pk=_KEY_ROW)


class SubscriptionError(ValueError):
    """The browser gave a subscription the panel cannot send to."""


class BrowserSubscriptions:
    """The browsers of one user: subscribe, unsubscribe, check."""

    LIMIT = 10
    # The session keeps the address its browser subscribed with: signing out unsubscribes it.
    SESSION_KEY = "push_endpoint"

    def __init__(self, user: User) -> None:
        self._user = user

    def add(self, endpoint: str, p256dh: str, auth: str, user_agent: str = "") -> None:
        """Save the browser's subscription; it moves to this user if another one had it.

        Raises SubscriptionError for an address or keys the panel cannot send to.
        """
        self._check(endpoint, p256dh, auth)
        with transaction.atomic():
            PushSubscription.objects.update_or_create(
                endpoint=endpoint,
                defaults={
                    "user": self._user,
                    "p256dh": p256dh,
                    "auth": auth,
                    "device": device_name(user_agent),
                },
            )
            # The oldest go beyond the limit: a browser that changed its address left one behind.
            stale = self._mine().values_list("pk", flat=True)[self.LIMIT :]
            PushSubscription.objects.filter(pk__in=list(stale)).delete()

    def replace(
        self, old_endpoint: str, endpoint: str, p256dh: str, auth: str, user_agent: str = ""
    ) -> None:
        """Put the browser's new subscription in place of its old one, which must be this user's.

        Raises SubscriptionError when the old one is not, or for a new one the panel cannot send to;
        then nothing changes.
        """
        with transaction.atomic():
            old = self._mine().select_for_update().filter(endpoint=old_endpoint)
            if not old.exists():
                raise SubscriptionError("the old subscription is not this user's")
            old.delete()
            self.add(endpoint, p256dh, auth, user_agent)

    def remove(self, endpoint: str) -> None:
        self._mine().filter(endpoint=endpoint).delete()

    def forget(self, pk: int) -> str | None:
        """Delete one of the user's subscriptions from the profile list; its address, or None when
        the user has no such subscription.
        """
        subscription = self._mine().filter(pk=pk).first()
        if subscription is None:
            return None
        subscription.delete()
        return subscription.endpoint

    def browsers(self) -> list[PushSubscription]:
        return list(self._mine())

    def has(self, endpoint: str) -> bool:
        return self._mine().filter(endpoint=endpoint).exists()

    def count(self) -> int:
        return self._mine().count()

    def _mine(self) -> QuerySet[PushSubscription]:
        return PushSubscription.objects.filter(user=self._user)

    @staticmethod
    def _check(endpoint: str, p256dh: str, auth: str) -> None:
        max_length = PushSubscription._meta.get_field("endpoint").max_length or 0
        if urlsplit(endpoint).scheme != "https" or len(endpoint) > max_length:
            raise SubscriptionError("the push address is not an https URL")
        try:
            AddressGuard.from_settings().check(endpoint)
        except BlockedAddressError:
            raise SubscriptionError("the push address is in a private network") from None
        try:
            public_key(p256dh)
            secret = unb64url(auth)
        except ValueError:
            raise SubscriptionError("the browser keys are not valid") from None
        if len(secret) != 16:  # noqa: PLR2004 - the size of the auth secret, RFC 8291
            raise SubscriptionError("the browser keys are not valid")


@dataclass(frozen=True)
class PushMessage:
    """What the service worker shows: title, text and the page a click opens."""

    title: str
    body: str
    url: str
    tag: str

    # The page a click opens, by event: a URL name and the detail that gives its argument.
    PAGES: ClassVar[dict[Event, tuple[str, str | None]]] = {
        Event.LIMIT_REACHED: ("portal:usage", None),
        Event.TRAFFIC_LOW: ("portal:usage", None),
        Event.TICKET_CREATED: ("support:inbox-conversation", "number"),
        Event.CHAT_MESSAGE: ("support:inbox-conversation", "number"),
        Event.INCIDENT_OPENED: ("support:incidents", None),
        Event.INCIDENT_CLOSED: ("support:incidents", None),
        Event.REGISTRATION_PENDING: ("accounts:registrations", None),
        Event.PAYMENT_RECEIVED: ("billing:orders", None),
        Event.SERVER_DOWN: ("servers:list", None),
        Event.SERVER_UP: ("servers:list", None),
        Event.AWG3_DROPPED: ("servers:list", None),
        Event.AWG3_RESTORED: ("servers:list", None),
        Event.SERVER_LOAD_HIGH: ("servers:list", None),
        Event.SERVER_LOAD_NORMAL: ("servers:list", None),
        Event.ADDRESS_BUSY: ("servers:list", None),
        Event.ADDRESS_CALM: ("servers:list", None),
        Event.INTERFACE_LOST: ("servers:list", None),
        Event.INTERFACE_RESTORED: ("servers:list", None),
        Event.INTERFACE_REPAIR_FAILED: ("servers:list", None),
    }

    @classmethod
    def of(cls, message: Message) -> Self:
        return cls(
            title=message.title[:_TITLE_LIMIT],
            body=message.summary[:_BODY_LIMIT],
            url=cls._page(message, message.link),
            tag=message.uid,
        )

    def payload(self) -> bytes:
        data = {"title": self.title, "body": self.body, "url": self.url, "tag": self.tag}
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()

    @classmethod
    def _page(cls, message: Message, url: str) -> str:
        """A path in the panel: the service worker opens it on its own origin."""
        if url:
            parts = urlsplit(url)
            return parts.path + (f"?{parts.query}" if parts.query else "")
        name, detail = cls.PAGES.get(Event(message.event), ("home", None))
        if detail is None:
            return reverse(name)
        value = message.data.get(detail)
        return reverse(name, args=[value]) if value else reverse("home")


class BrowserPush:
    """Sends a message to subscriptions: one push each, the dead ones deleted."""

    def __init__(
        self,
        http: HttpClient,
        keys: VapidKeys,
        subject: str,
        clock: Callable[[], datetime] = timezone.now,
    ) -> None:
        self._http = http
        self._keys = keys
        self._subject = subject
        self._clock = clock

    @classmethod
    def from_settings(cls, http: HttpClient) -> Self:
        return cls(http, PanelKeys.get(), settings.SITE_URL)

    def send(self, subscriptions: Iterable[PushSubscription], message: Message) -> None:
        """A push to every subscription. Done when any took it: a retry would repeat the message
        in the others. Else DeliveryError, retryable if any failure is.
        """
        payload = PushMessage.of(message).payload()
        errors: list[DeliveryError] = []
        taken = False
        for subscription in subscriptions:
            try:
                self._push(subscription, payload)
            except DeliveryError as error:
                if error.status in _GONE:
                    subscription.delete()
                errors.append(error)
            else:
                taken = True
                PushSubscription.objects.filter(pk=subscription.pk).update(
                    delivered_at=self._clock()
                )
        if taken:
            return
        if not errors:
            raise DeliveryError("no browser is subscribed", retryable=False)
        raise DeliveryError(
            "; ".join(map(str, errors)), retryable=any(error.retryable for error in errors)
        )

    def _push(self, subscription: PushSubscription, payload: bytes) -> None:
        try:
            body = BrowserKeys(subscription.p256dh, subscription.auth).encrypt(payload)
        except ValueError as error:
            raise DeliveryError(str(error), retryable=False) from None
        headers = {
            "Authorization": self._keys.authorization(
                subscription.endpoint, self._subject, self._clock()
            ),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(_TIME_TO_LIVE),
            "Urgency": "normal",
        }
        self._http.post(subscription.endpoint, body, headers)


@register
class BrowserChannel(NotificationChannel):
    """The browsers the active staff subscribed in their profiles: the administrators get every
    event, a moderator or an owner those of their zone (`StaffAudience`)."""

    kind = ChannelKind.BROWSER

    def __init__(self, push: BrowserPush) -> None:
        self._push = push

    @classmethod
    def from_model(cls, channel: Channel, http: HttpClient) -> Self:
        return cls(BrowserPush.from_settings(http))

    def send(self, message: Message) -> None:
        members = StaffAudience(message).members()
        subscriptions = PushSubscription.objects.filter(user__in=members).order_by("pk")
        self._push.send(subscriptions, message)


def push_to_user(user: User, message: Message, http: HttpClient) -> None:
    """The message to the browsers `user` subscribed."""
    BrowserPush.from_settings(http).send(user.push_subscriptions.order_by("pk"), message)
