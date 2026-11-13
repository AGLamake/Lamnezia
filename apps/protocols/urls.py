from django.urls import path

from apps.protocols.views import (
    AwgMigrationFinishView,
    AwgMigrationView,
    AwgUpgradeView,
    ContainerActionView,
    ContainerRemoveView,
    DnsRebuildView,
    IpsecReissueView,
    OffloadView,
    ProtocolSettingsView,
    ReaddressView,
    ServerImportView,
    ServerInstallView,
    ServiceActionView,
    ServiceLogDeletionView,
    ServicePasswordView,
    ServiceRemoveView,
    ServiceSecretView,
    ServiceSettingsView,
    SpeedTestStopView,
    SpeedTestView,
)

app_name = "protocols"

urlpatterns = [
    path("import/<int:server_pk>/", ServerImportView.as_view(), name="import"),
    path("install/<int:server_pk>/", ServerInstallView.as_view(), name="install"),
    path("readdress/<int:server_pk>/", ReaddressView.as_view(), name="readdress"),
    path("offload/<int:server_pk>/<slug:action>/", OffloadView.as_view(), name="offload"),
    path("speed-test/<int:server_pk>/", SpeedTestView.as_view(), name="speed-test"),
    path("speed-test/<int:server_pk>/stop/", SpeedTestStopView.as_view(), name="speed-test-stop"),
    path("<int:pk>/ipsec/reissue/", IpsecReissueView.as_view(), name="ipsec-reissue"),
    path("<int:pk>/settings/", ProtocolSettingsView.as_view(), name="settings"),
    path("<int:pk>/awg-upgrade/", AwgUpgradeView.as_view(), name="awg-upgrade"),
    path("<int:pk>/awg-migration/", AwgMigrationView.as_view(), name="awg-migration"),
    path(
        "<int:pk>/awg-migration/finish/",
        AwgMigrationFinishView.as_view(),
        name="awg-migration-finish",
    ),
    path("<int:pk>/container/remove/", ContainerRemoveView.as_view(), name="remove"),
    path("<int:pk>/container/<slug:action>/", ContainerActionView.as_view(), name="container"),
    path("services/<int:pk>/settings/", ServiceSettingsView.as_view(), name="service-settings"),
    path("services/<int:pk>/remove/", ServiceRemoveView.as_view(), name="service-remove"),
    path("services/<int:pk>/secret/", ServiceSecretView.as_view(), name="service-secret"),
    path("services/<int:pk>/password/", ServicePasswordView.as_view(), name="service-password"),
    path("services/<int:pk>/app-log/", ServiceLogDeletionView.as_view(), name="service-app-log"),
    path("services/<int:pk>/rebuild-dns/", DnsRebuildView.as_view(), name="service-rebuild-dns"),
    path("services/<int:pk>/<slug:action>/", ServiceActionView.as_view(), name="service-container"),
]
