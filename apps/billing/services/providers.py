"""Payment providers: the contract a billing plugin implements and the registry of plugins.

A plugin is a Django app listed in LAMNEZIA_PLUGINS; its `AppConfig.ready()` calls
`payment_providers.register(MyProvider())`. Nothing else in the core changes.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse
from django.urls import URLPattern, URLResolver

from apps.billing.models import Order

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise


@dataclass(frozen=True)
class CheckoutUrls:
    """Absolute addresses for the payment service: where the user comes back, where to report."""

    return_url: str
    notify_url: str


@dataclass(frozen=True)
class PaymentNotice:
    """What a provider read from a notification it has checked.

    `amount` and `currency` are what was actually paid; the core compares them with the order.
    """

    order_number: UUID
    paid: bool
    amount: Decimal | None = None
    currency: str = ""
    external_id: str = ""


class InvalidNotificationError(Exception):
    """The notification is not from the payment service or cannot be read."""


class PaymentProviderError(Exception):
    """The payment service did not accept the order; the user is asked to try again later."""


class PaymentProvider(ABC):
    """One way to pay. `key` is stable: it is stored in orders and is part of addresses.

    - `start` registers the order with the payment service and returns the address to send
      the user to; it may set `order.external_id`, which the core saves.
    - `parse_notification` must prove the notification comes from the payment service
      (signature, secret, a request back to the service) before reading it, and raise
      `InvalidNotificationError` otherwise: the notify address is open to anyone.
    - The core matches the amount and currency with the order and applies a payment once,
      however many times it is reported.
    - `refund` is optional: a provider that can return money sets `refunds = True` and
      implements it; an administrator then sees **Refund** on a paid order.
    """

    key: ClassVar[str]
    title: ClassVar["StrOrPromise"]
    refunds: ClassVar[bool] = False

    def is_available(self) -> bool:
        """False hides the provider, for example while its settings are missing."""
        return True

    @abstractmethod
    def start(self, order: Order, urls: CheckoutUrls) -> str: ...

    @abstractmethod
    def parse_notification(self, request: HttpRequest) -> PaymentNotice: ...

    def acknowledge(self, request: HttpRequest, order: Order) -> HttpResponse:
        """The answer the payment service expects to a notification that was applied."""
        return HttpResponse("OK", content_type="text/plain")

    def refund(self, order: Order) -> None:
        """Return the money of a paid order; raise `PaymentProviderError` if the service refuses."""
        raise NotImplementedError

    def urls(self) -> Sequence[URLPattern | URLResolver]:
        """Pages of the plugin itself, served under /billing/<key>/ in the namespace `key`."""
        return ()


class PaymentProviders:
    """The registered providers by key."""

    def __init__(self) -> None:
        self._providers: dict[str, PaymentProvider] = {}

    def register(self, provider: PaymentProvider) -> None:
        if provider.key in self._providers:
            raise ImproperlyConfigured(f"Payment provider {provider.key!r} is registered twice.")
        self._providers[provider.key] = provider

    def unregister(self, key: str) -> None:
        del self._providers[key]

    def all(self) -> list[PaymentProvider]:
        return list(self._providers.values())

    def available(self) -> list[PaymentProvider]:
        return [provider for provider in self._providers.values() if provider.is_available()]

    def get(self, key: str) -> PaymentProvider | None:
        """The provider if it is registered and available now."""
        provider = self._providers.get(key)
        return provider if provider and provider.is_available() else None


payment_providers = PaymentProviders()
