from django.urls import path

from apps.groups.views import (
    GroupCreateView,
    GroupDeleteView,
    GroupDetailView,
    GroupJoinView,
    GroupLeaveView,
    GroupListView,
)

app_name = "groups"

urlpatterns = [
    path("", GroupListView.as_view(), name="list"),
    path("add/", GroupCreateView.as_view(), name="add"),
    path("<int:pk>/", GroupDetailView.as_view(), name="detail"),
    path("<int:pk>/delete/", GroupDeleteView.as_view(), name="delete"),
    path("<int:pk>/join/<int:instance_pk>/", GroupJoinView.as_view(), name="join"),
    path("members/<int:member_pk>/leave/", GroupLeaveView.as_view(), name="leave"),
]
