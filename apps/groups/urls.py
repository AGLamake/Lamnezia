from django.urls import path

from apps.groups.views import (
    GroupCreateView,
    GroupDeleteView,
    GroupDetailView,
    GroupEditView,
    GroupHandoverView,
    GroupInstallView,
    GroupJoinView,
    GroupLeaveRetryView,
    GroupLeaveView,
    GroupListView,
    GroupSettingsView,
    GroupStayView,
)

app_name = "groups"

urlpatterns = [
    path("", GroupListView.as_view(), name="list"),
    path("add/", GroupCreateView.as_view(), name="add"),
    path("<int:pk>/", GroupDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", GroupEditView.as_view(), name="edit"),
    path("<int:pk>/settings/", GroupSettingsView.as_view(), name="settings"),
    path("<int:pk>/delete/", GroupDeleteView.as_view(), name="delete"),
    path("<int:pk>/join/<int:instance_pk>/", GroupJoinView.as_view(), name="join"),
    path("<int:pk>/install/<int:server_pk>/", GroupInstallView.as_view(), name="install"),
    path("members/<int:member_pk>/leave/", GroupLeaveView.as_view(), name="leave"),
    path("members/<int:member_pk>/leave/retry/", GroupLeaveRetryView.as_view(), name="leave_retry"),
    path("members/<int:member_pk>/stay/", GroupStayView.as_view(), name="stay"),
    path("members/<int:member_pk>/lead/", GroupHandoverView.as_view(), name="lead"),
]
