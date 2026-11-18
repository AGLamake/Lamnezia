"""Devices of a subscription link: remembered by `x-hwid`, limited by the plan, forgotten.

Clients such as Happ send the device ID and what the device is with every fetch. The limit only
gates the feed: the keys on the XRay server stay as they are, and a client that already has a
key keeps working with it. Request addresses are not stored.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import User
from apps.core.services.site_settings import cached_settings
from apps.keys.models import Subscription, SubscriptionDevice
from apps.plans.services.limits import effective_limits

_FIELD_LIMIT = 100


@dataclass(frozen=True, repr=False)
class DeviceSignature:
    """What a client says about its device in the fetch headers; the ID itself is a secret."""

    hwid: str
    model: str = ""
    system: str = ""
    app: str = ""

    @classmethod
    def of(cls, headers: Mapping[str, str]) -> "DeviceSignature | None":
        """None when the client sends no `x-hwid`."""
        hwid = headers.get("x-hwid", "").strip()
        if not hwid:
            return None
        system = " ".join(
            part for part in (headers.get("x-device-os", ""), headers.get("x-ver-os", "")) if part
        )
        return cls(
            hwid,
            headers.get("x-device-model", "").strip()[:_FIELD_LIMIT],
            system.strip()[:_FIELD_LIMIT],
            headers.get("User-Agent", "").strip()[:_FIELD_LIMIT],
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.hwid.encode()).hexdigest()


class Refusal(Enum):
    """Why a fetch gets no keys; the text is for the person who sees it in the client."""

    NO_DEVICE_ID = _(
        "This app does not send a device ID, and the panel gives the subscription only to apps "
        "that do. Use another app, e.g. Happ."
    )
    TOO_MANY_DEVICES = _(
        "The subscription is already used on as many devices as the plan allows. Forget a "
        "device on the subscription page of the portal, then refresh."
    )

    @property
    def text(self) -> str:
        return str(self.value)


class SubscriptionDevices:
    """The devices of one user's subscription link."""

    def __init__(self, user: User) -> None:
        self._user = user

    def all(self) -> list[SubscriptionDevice]:
        return list(SubscriptionDevice.objects.filter(subscription__user=self._user))

    def forget(self, device_id: str) -> bool:
        """The device may fetch again and takes a free place then. `device_id` as a form sent it."""
        if not device_id.isdigit():
            return False
        deleted, _rows = SubscriptionDevice.objects.filter(
            subscription__user=self._user, pk=device_id
        ).delete()
        return bool(deleted)

    def admit(self, device: DeviceSignature | None) -> Refusal | None:
        """Remember the device of a fetch; a refusal when it may not get the keys."""
        if device is None:
            allowed = cached_settings.get().subscription_without_device_id
            return None if allowed else Refusal.NO_DEVICE_ID
        now = timezone.now()
        details = {"model": device.model, "system": device.system, "app": device.app}
        with transaction.atomic():
            # The lock keeps two new devices fetching at once from both taking the last place.
            subscription = Subscription.objects.select_for_update().get(user=self._user)
            known = subscription.devices.filter(digest=device.digest)
            if known.update(last_seen=now, **details):
                return None
            if not effective_limits(self._user).allows_another_device(subscription.devices.count()):
                return Refusal.TOO_MANY_DEVICES
            subscription.devices.create(digest=device.digest, last_seen=now, **details)
        return None
