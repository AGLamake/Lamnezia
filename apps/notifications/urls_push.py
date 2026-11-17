"""Addresses of notifications in the browser: for every signed-in user, not only the staff."""

from django.urls import path

from apps.notifications.views_push import (
    PushStateView,
    PushSubscribeView,
    PushTestView,
    PushUnsubscribeView,
)

app_name = "push"

urlpatterns = [
    path("subscribe/", PushSubscribeView.as_view(), name="subscribe"),
    path("unsubscribe/", PushUnsubscribeView.as_view(), name="unsubscribe"),
    path("state/", PushStateView.as_view(), name="state"),
    path("test/", PushTestView.as_view(), name="test"),
]
