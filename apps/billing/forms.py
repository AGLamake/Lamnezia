from typing import Any, cast

from django import forms
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.billing.models import Order, OrderStatus
from apps.billing.services.orders import plans_for_sale
from apps.billing.services.providers import PaymentProvider, payment_providers


class CheckoutForm(forms.Form):
    """A plan for sale and an available way to pay for it."""

    plan = forms.ModelChoiceField(queryset=plans_for_sale())
    provider = forms.ChoiceField()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        cast(forms.ChoiceField, self.fields["provider"]).choices = [
            (provider.key, provider.title) for provider in payment_providers.available()
        ]

    @property
    def payment_provider(self) -> PaymentProvider:
        provider = payment_providers.get(self.cleaned_data["provider"])
        assert provider is not None
        return provider


class OrderFilterForm(forms.Form):
    """Filters of the administrator's list of orders."""

    status = forms.ChoiceField(
        label=_("Status"), required=False, choices=[("", _("Any")), *OrderStatus.choices]
    )
    provider = forms.ChoiceField(label=_("Payment method"), required=False)
    user = forms.CharField(label=_("User"), required=False)
    since = forms.DateField(
        label=_("From"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    until = forms.DateField(
        label=_("To"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Orders keep the key of a provider that may be gone since; its title, while it is there.
        titles = {provider.key: provider.title for provider in payment_providers.all()}
        used = Order.objects.order_by("provider").values_list("provider", flat=True).distinct()
        cast(forms.ChoiceField, self.fields["provider"]).choices = [
            ("", _("Any")),
            *((key, titles.get(key, key)) for key in used),
        ]

    def filter(self, orders: QuerySet[Order]) -> QuerySet[Order]:
        if not self.is_valid():
            return orders
        data = self.cleaned_data
        if data["status"]:
            orders = orders.filter(status=data["status"])
        if data["provider"]:
            orders = orders.filter(provider=data["provider"])
        if data["user"]:
            name = data["user"]
            orders = orders.filter(Q(username__icontains=name) | Q(user__username__icontains=name))
        if data["since"]:
            orders = orders.filter(created_at__date__gte=data["since"])
        if data["until"]:
            orders = orders.filter(created_at__date__lte=data["until"])
        return orders
