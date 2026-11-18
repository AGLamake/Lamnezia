"""Addresses of the API token page under /settings/api/."""

from django.urls import path

from apps.api import views_admin

app_name = "api-tokens"

urlpatterns = [
    path("", views_admin.TokenListView.as_view(), name="list"),
    path("<int:pk>/disable/", views_admin.TokenDeactivateView.as_view(), name="deactivate"),
]
