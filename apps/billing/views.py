from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBadRequest
from django.http.response import HttpResponseBase
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import DetailView, TemplateView

from apps.accounts.models import User
from apps.audit.services.journal import client_ip
from apps.billing.forms import CheckoutForm
from apps.billing.models import Order
from apps.billing.services.orders import (
    Checkout,
    CheckoutRefusedError,
    PaymentMismatchError,
    UnknownOrderError,
    plans_for_sale,
    settle,
)
from apps.billing.services.providers import (
    CheckoutUrls,
    InvalidNotificationError,
    PaymentProviderError,
    payment_providers,
)

_RECENT_ORDERS = 20


class _BillingView(LoginRequiredMixin):
    """A page of the user's own payments; without an available provider there is none."""

    request: HttpRequest

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        # A guest is sent to sign in first, as on every private page.
        if request.user.is_authenticated and not payment_providers.available():
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    @property
    def user(self) -> User:
        assert isinstance(self.request.user, User)
        return self.request.user


class BillingView(_BillingView, TemplateView):
    template_name = "billing/index.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(
            plans=plans_for_sale(),
            providers=payment_providers.available(),
            orders=self.user.orders.select_related("plan")[:_RECENT_ORDERS],
            can_buy=self.user.can_have_keys,
            **kwargs,
        )


class CheckoutView(_BillingView, View):
    def post(self, request: HttpRequest) -> HttpResponse:
        form = CheckoutForm(request.POST)
        if not form.is_valid():
            messages.error(request, _("Choose a plan and a way to pay."))
            return redirect("billing:index")
        try:
            address = Checkout(self.user, client_ip(request)).start(
                form.cleaned_data["plan"], form.payment_provider, self._urls
            )
        except CheckoutRefusedError:
            messages.error(request, _("This plan cannot be bought now."))
            return redirect("billing:index")
        except PaymentProviderError:
            messages.error(request, _("The payment service is not answering. Try again later."))
            return redirect("billing:index")
        return redirect(address)

    def _urls(self, order: Order) -> CheckoutUrls:
        build = self.request.build_absolute_uri
        return CheckoutUrls(
            return_url=build(reverse("billing:order", args=[order.number])),
            notify_url=build(reverse("billing:notify", args=[order.provider])),
        )


class OrderView(_BillingView, DetailView):  # type: ignore[type-arg]
    template_name = "billing/order.html"
    slug_field = "number"
    slug_url_kwarg = "number"

    def get_queryset(self) -> Any:
        return self.user.orders.select_related("plan")


@method_decorator(csrf_exempt, name="dispatch")
class NotifyView(View):
    """Where a payment service reports payments; the provider proves who is speaking."""

    def post(self, request: HttpRequest, provider: str) -> HttpResponse:
        payment = payment_providers.get(provider)
        if payment is None:
            raise Http404
        try:
            notice = payment.parse_notification(request)
            order = settle(payment.key, notice, timezone.now())
        except (InvalidNotificationError, UnknownOrderError, PaymentMismatchError):
            return HttpResponseBadRequest()
        return payment.acknowledge(request, order)
