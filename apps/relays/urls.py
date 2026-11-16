from django.urls import path

from apps.relays.views import (
    RelayInstallView,
    RelayRemoveView,
    RelaySettingsView,
    RelayUpdateView,
)

app_name = "relays"

urlpatterns = [
    path("<int:pk>/install/", RelayInstallView.as_view(), name="install"),
    path("<int:pk>/update/", RelayUpdateView.as_view(), name="update"),
    path("<int:pk>/remove/", RelayRemoveView.as_view(), name="remove"),
    path("<int:pk>/settings/", RelaySettingsView.as_view(), name="settings"),
]
