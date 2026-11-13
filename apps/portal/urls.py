from django.urls import path

from apps.portal.views import (
    GetKeyView,
    KeyDeleteView,
    KeyReissueView,
    KeyRenameView,
    KeyRevealView,
    KeyStateView,
    KeyView,
    PortalHomeView,
    ServerStatusView,
    SubscriptionView,
    UsageView,
)

app_name = "portal"

urlpatterns = [
    path("", PortalHomeView.as_view(), name="index"),
    path("usage/", UsageView.as_view(), name="usage"),
    path("servers/", ServerStatusView.as_view(), name="servers"),
    path("subscription/", SubscriptionView.as_view(), name="subscription"),
    path("keys/new/", GetKeyView.as_view(), name="get-key"),
    path("keys/<int:pk>/", KeyView.as_view(), name="key"),
    path("keys/<int:pk>/state/", KeyStateView.as_view(), name="key-state"),
    path("keys/<int:pk>/show/", KeyRevealView.as_view(), name="reveal"),
    path("keys/<int:pk>/rename/", KeyRenameView.as_view(), name="rename"),
    path("keys/<int:pk>/reissue/", KeyReissueView.as_view(), name="reissue"),
    path("keys/<int:pk>/delete/", KeyDeleteView.as_view(), name="delete"),
]
