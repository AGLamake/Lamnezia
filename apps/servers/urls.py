from django.urls import path

from apps.servers.views import (
    ProbeInstallView,
    ProbeRemoveView,
    ServerAddressAddView,
    ServerAddressCheckView,
    ServerAddressRemoveView,
    ServerCheckView,
    ServerCreateView,
    ServerDeleteView,
    ServerDetailView,
    ServerHostKeyForgetView,
    ServerLeaveView,
    ServerListView,
    ServerStealthView,
    ServerUpdateView,
)

app_name = "servers"

urlpatterns = [
    path("", ServerListView.as_view(), name="list"),
    path("add/", ServerCreateView.as_view(), name="add"),
    path("<int:pk>/", ServerDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", ServerUpdateView.as_view(), name="edit"),
    path("<int:pk>/check/", ServerCheckView.as_view(), name="check"),
    path("<int:pk>/stealth/", ServerStealthView.as_view(), name="stealth"),
    path("<int:pk>/delete/", ServerDeleteView.as_view(), name="delete"),
    path("<int:pk>/leave/", ServerLeaveView.as_view(), name="leave"),
    path("<int:pk>/host-key/forget/", ServerHostKeyForgetView.as_view(), name="forget-host-key"),
    path("<int:pk>/addresses/", ServerAddressAddView.as_view(), name="address-add"),
    path(
        "<int:pk>/addresses/<int:address_pk>/check/",
        ServerAddressCheckView.as_view(),
        name="address-check",
    ),
    path(
        "<int:pk>/addresses/<int:address_pk>/remove/",
        ServerAddressRemoveView.as_view(),
        name="address-remove",
    ),
    path("<int:pk>/probe/", ProbeInstallView.as_view(), name="probe-install"),
    path("<int:pk>/probe/remove/", ProbeRemoveView.as_view(), name="probe-remove"),
]
