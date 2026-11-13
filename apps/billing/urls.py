"""Billing addresses: the user's payment pages, the administrator's orders, notifications, and the
pages of each plugin.
"""

from django.urls import URLPattern, URLResolver, include, path

from apps.billing.services.providers import payment_providers
from apps.billing.views import BillingView, CheckoutView, NotifyView, OrderView
from apps.billing.views_admin import OrderListView, OrderRefundView

app_name = "billing"

urlpatterns: list[URLPattern | URLResolver] = [
    path("", BillingView.as_view(), name="index"),
    path("checkout/", CheckoutView.as_view(), name="checkout"),
    path("orders/", OrderListView.as_view(), name="orders"),
    path("orders/<uuid:number>/", OrderView.as_view(), name="order"),
    path("orders/<uuid:number>/refund/", OrderRefundView.as_view(), name="refund"),
    path("notify/<slug:provider>/", NotifyView.as_view(), name="notify"),
    # Plugins register their providers in AppConfig.ready(), before the URLconf is loaded.
    *(
        path(f"{provider.key}/", include((list(provider.urls()), provider.key)))
        for provider in payment_providers.all()
    ),
]
