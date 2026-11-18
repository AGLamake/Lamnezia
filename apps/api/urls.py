"""Addresses of the API under /api/v1/; calls carry a token, not a session, so CSRF is off."""

from collections.abc import Callable
from typing import Any

from django.http import HttpResponse
from django.urls import path, re_path
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.api import views

app_name = "api"


def _endpoint(view: type[View]) -> Callable[..., HttpResponse]:
    endpoint: Callable[..., Any] = view.as_view()
    return csrf_exempt(endpoint)


urlpatterns = [
    path("openapi.json", _endpoint(views.OpenApiView), name="openapi"),
    path("token", _endpoint(views.TokenView), name="token"),
    path("servers", _endpoint(views.ServerListView), name="servers"),
    path("servers/<int:pk>", _endpoint(views.ServerDetailView), name="server"),
    path("users", _endpoint(views.UserListView), name="users"),
    path("users/<int:pk>", _endpoint(views.UserDetailView), name="user"),
    path("keys", _endpoint(views.KeyListView), name="keys"),
    path("keys/<int:pk>", _endpoint(views.KeyDetailView), name="key"),
    path("traffic", _endpoint(views.TrafficView), name="traffic"),
    re_path(r"^(?P<path>.*)$", csrf_exempt(views.not_found)),
]
