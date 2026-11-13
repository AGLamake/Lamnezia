from django.urls import path

from apps.notifications.views import (
    ChannelCreateView,
    ChannelDeleteView,
    ChannelListView,
    ChannelTestView,
    ChannelUpdateView,
    DeliveryLogView,
)

app_name = "notifications"

urlpatterns = [
    path("", ChannelListView.as_view(), name="channels"),
    path("add/<str:kind>/", ChannelCreateView.as_view(), name="add"),
    path("<int:pk>/edit/", ChannelUpdateView.as_view(), name="edit"),
    path("<int:pk>/delete/", ChannelDeleteView.as_view(), name="delete"),
    path("<int:pk>/test/", ChannelTestView.as_view(), name="test"),
    path("log/", DeliveryLogView.as_view(), name="log"),
]
