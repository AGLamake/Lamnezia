from django.urls import path

from apps.updates.views import (
    CheckReleaseView,
    PanelUpdateStatusView,
    PanelUpdateView,
    RollbackRestoreView,
)

app_name = "updates"

urlpatterns = [
    path("", PanelUpdateView.as_view(), name="update"),
    path("status/", PanelUpdateStatusView.as_view(), name="status"),
    path("check/", CheckReleaseView.as_view(), name="check"),
    path("restore/", RollbackRestoreView.as_view(), name="restore"),
]
