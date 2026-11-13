from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.views.generic import DetailView

from apps.billing.models import Order, OrderStatus
from apps.billing.services.providers import payment_providers
from examples.billing_dummy.provider import DummyProvider, sign_notice


class PayView(LoginRequiredMixin, DetailView):  # type: ignore[type-arg]
    """The page of the pretend payment service: pay or refuse an order of the user."""

    template_name = "billing_dummy/pay.html"
    slug_field = "number"
    slug_url_kwarg = "number"

    def get_queryset(self) -> Any:
        if payment_providers.get(DummyProvider.key) is None:
            raise Http404
        return Order.objects.filter(
            user=self.request.user.pk, provider=DummyProvider.key, status=OrderStatus.PENDING
        ).select_related("plan")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        order = self.object
        return super().get_context_data(
            paid_notice=sign_notice(order, paid=True),
            failed_notice=sign_notice(order, paid=False),
            **kwargs,
        )
