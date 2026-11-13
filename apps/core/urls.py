from django.urls import path

from apps.core.views import SiteSettingsView

app_name = "core"

urlpatterns = [
    path("", SiteSettingsView.as_view(), name="settings"),
]
