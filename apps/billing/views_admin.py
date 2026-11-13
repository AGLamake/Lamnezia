"""Orders in the administrator's panel: the filtered list and refunds."""

from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import ListView

from apps.accounts.permissions import AdminRequiredMixin
from apps.billing.forms import OrderFilterForm
from apps.billing.models import Order
from apps.billing.services.orders import RefundRefusedError, refund
from apps.billing.services.providers import PaymentProviderError, payment_providers
from apps.core.paging import PageSizeMixin


class OrderListView(PageSizeMixin, AdminRequiredMixin, ListView):  # type: ignore[type-arg]
    template_name = "billing/orders.html"
    context_object_name = "orders"

    def get_queryset(self) -> Any:
        self.filter_form = OrderFilterForm(self.request.GET or None)
        return self.filter_form.filter(Order.objects.select_related("user", "plan"))

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(
            filter_form=self.filter_form,
            refundable=[p.key for p in payment_providers.available() if p.refunds],
            **kwargs,
        )


class OrderRefundView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, number: str) -> HttpResponse:
        order = get_object_or_404(Order, number=number)
        try:
            refund(order, self.member, self.client_ip)
        except RefundRefusedError:
            messages.error(request, _("This order cannot be refunded."))
        except PaymentProviderError:
            messages.error(request, _("The payment service is not answering. Try again later."))
        else:
            messages.success(
                request, _("The money is returned. Change the plan of the user if needed.")
            )
        return redirect("billing:orders")
