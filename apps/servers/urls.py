from django.urls import path

from apps.servers.views import (
    ProbeInstallView,
    ProbeRemoveView,
    ServerCheckView,
    ServerCreateView,
    ServerDetailView,
    ServerHostKeyForgetView,
    ServerListView,
    ServerUpdateView,
)

app_name = "servers"

urlpatterns = [
    path("", ServerListView.as_view(), name="list"),
    path("add/", ServerCreateView.as_view(), name="add"),
    path("<int:pk>/", ServerDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", ServerUpdateView.as_view(), name="edit"),
    path("<int:pk>/check/", ServerCheckView.as_view(), name="check"),
    path("<int:pk>/host-key/forget/", ServerHostKeyForgetView.as_view(), name="forget-host-key"),
    path("<int:pk>/probe/", ProbeInstallView.as_view(), name="probe-install"),
    path("<int:pk>/probe/remove/", ProbeRemoveView.as_view(), name="probe-remove"),
]
