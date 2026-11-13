"""The test payment provider: the payment page is in the panel, the notification is signed.

A real provider sends the user to the payment service and gets its notification at
`CheckoutUrls.notify_url`. Here the page of the plugin plays the service: its form posts a
notification signed with the panel's secret key, so the core path is the same as for real money.
"""

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.conf import settings
from django.core import signing
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import URLPattern, URLResolver, path, reverse
from django.utils.translation import gettext_lazy as _

from apps.billing.models import Order
from apps.billing.services.providers import (
    CheckoutUrls,
    InvalidNotificationError,
    PaymentNotice,
    PaymentProvider,
)

_SIGNER = signing.Signer(salt="examples.billing_dummy")


def sign_notice(order: Order, *, paid: bool) -> str:
    """What the payment page posts: the order, the amount and the result, signed."""
    return _SIGNER.sign_object(
        {
            "order": str(order.number),
            "amount": str(order.amount),
            "currency": order.currency,
            "paid": paid,
        }
    )


class DummyProvider(PaymentProvider):
    key = "dummy"
    title = _("Test payment")

    def is_available(self) -> bool:
        return bool(settings.DEBUG)

    def start(self, order: Order, urls: CheckoutUrls) -> str:
        order.external_id = f"dummy-{order.number.hex[:12]}"
        return reverse("billing:dummy:pay", args=[order.number])

    def parse_notification(self, request: HttpRequest) -> PaymentNotice:
        try:
            data = _SIGNER.unsign_object(request.POST.get("notice", ""))
            return PaymentNotice(
                order_number=UUID(data["order"]),
                paid=bool(data["paid"]),
                amount=Decimal(data["amount"]),
                currency=data["currency"],
            )
        except (signing.BadSignature, KeyError, TypeError, ValueError, InvalidOperation):
            raise InvalidNotificationError from None

    def acknowledge(self, request: HttpRequest, order: Order) -> HttpResponse:
        # A real service reads this answer itself; here it is the user's browser.
        return redirect("billing:order", number=order.number)

    def urls(self) -> Sequence[URLPattern | URLResolver]:
        from examples.billing_dummy.views import PayView  # noqa: PLC0415

        return [path("pay/<uuid:number>/", PayView.as_view(), name="pay")]
