from django.urls import path

from apps.backups.views import ProtocolRestoreView, ServiceRestoreView, SnapshotRestoreView

app_name = "backups"

urlpatterns = [
    path("restore/<int:instance_pk>/", ProtocolRestoreView.as_view(), name="restore"),
    path(
        "restore-snapshot/<int:snapshot_pk>/",
        SnapshotRestoreView.as_view(),
        name="restore-snapshot",
    ),
    path("restore-service/<int:service_pk>/", ServiceRestoreView.as_view(), name="restore-service"),
]
