import json
from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID

import pytest
from django.http import HttpRequest

from apps.billing.models import Order
from apps.billing.services.providers import (
    CheckoutUrls,
    InvalidNotificationError,
    PaymentNotice,
    PaymentProvider,
    PaymentProviderError,
    payment_providers,
)

# Paying switches keys on the same fake server as the key tests.
from apps.keys.tests.conftest import awg_server  # noqa: F401

SECRET = "s3cret"


class FakeProvider(PaymentProvider):
    """A payment service that answers in JSON and proves itself with a shared secret."""

    key = "fake"
    title = "Fake pay"

    def __init__(self) -> None:
        self.started: list[CheckoutUrls] = []
        self.refuses = False

    def start(self, order: Order, urls: CheckoutUrls) -> str:
        if self.refuses:
            raise PaymentProviderError
        self.started.append(urls)
        order.external_id = f"ext-{order.pk}"
        return f"https://pay.example.com/{order.number}"

    def parse_notification(self, request: HttpRequest) -> PaymentNotice:
        if request.headers.get("X-Secret") != SECRET:
            raise InvalidNotificationError
        data = json.loads(request.body)
        return notice(data["order"], paid=data["paid"], amount=data["amount"], currency="RUB")


def notice(
    order: str | UUID, *, paid: bool = True, amount: str = "100", currency: str = "RUB"
) -> PaymentNotice:
    return PaymentNotice(UUID(str(order)), paid, Decimal(amount), currency, "pay-42")


@pytest.fixture
def provider() -> Iterator[FakeProvider]:
    fake = FakeProvider()
    payment_providers.register(fake)
    yield fake
    payment_providers.unregister(fake.key)
