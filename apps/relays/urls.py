from django.urls import path

from apps.relays.views import (
    RelayInstallView,
    RelayRemoveView,
    RelaySettingsView,
    RelayUpdateView,
    SplitEntryAddView,
    SplitEntryDeleteView,
    SplitListModeView,
    SplitSourceAddView,
    SplitSourceDeleteView,
    SplitSourceToggleView,
)

app_name = "relays"

urlpatterns = [
    path("<int:pk>/install/", RelayInstallView.as_view(), name="install"),
    path("<int:pk>/update/", RelayUpdateView.as_view(), name="update"),
    path("<int:pk>/remove/", RelayRemoveView.as_view(), name="remove"),
    path("<int:pk>/settings/", RelaySettingsView.as_view(), name="settings"),
    path("groups/<int:pk>/list-mode/", SplitListModeView.as_view(), name="list_mode"),
    path("groups/<int:pk>/sources/add/", SplitSourceAddView.as_view(), name="source_add"),
    path("sources/<int:pk>/toggle/", SplitSourceToggleView.as_view(), name="source_toggle"),
    path("sources/<int:pk>/delete/", SplitSourceDeleteView.as_view(), name="source_delete"),
    path("groups/<int:pk>/entries/add/", SplitEntryAddView.as_view(), name="entry_add"),
    path("entries/<int:pk>/delete/", SplitEntryDeleteView.as_view(), name="entry_delete"),
]
