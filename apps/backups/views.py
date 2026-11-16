from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.permissions import AdminRequiredMixin
from apps.backups.errors import RestoreError
from apps.backups.models import ProtocolSnapshot
from apps.backups.services.restore import start_restore, start_service_restore
from apps.backups.services.snapshot_restore import delete_snapshot, start_snapshot_restore
from apps.protocols.models import ProtocolInstance, ServiceInstance


class ProtocolRestoreView(AdminRequiredMixin, View):
    """Start restoring the protocol on its server; the server card shows the progress."""

    def post(self, request: HttpRequest, instance_pk: int) -> HttpResponse:
        instance = get_object_or_404(ProtocolInstance, pk=instance_pk)
        start_restore(instance, self.member, self.client_ip)
        return redirect("servers:detail", pk=instance.server_id)


class ServiceRestoreView(AdminRequiredMixin, View):
    """Start restoring the service on its server from the saved copy."""

    def post(self, request: HttpRequest, service_pk: int) -> HttpResponse:
        service = get_object_or_404(ServiceInstance, pk=service_pk)
        start_service_restore(service, self.member, self.client_ip)
        return redirect("servers:detail", pk=service.server_id)


class SnapshotRestoreView(AdminRequiredMixin, View):
    """Start putting the files of a snapshot back on its server; refused while in a group."""

    def post(self, request: HttpRequest, snapshot_pk: int) -> HttpResponse:
        snapshot = get_object_or_404(
            ProtocolSnapshot.objects.select_related("instance"), pk=snapshot_pk
        )
        try:
            start_snapshot_restore(snapshot, self.member, self.client_ip)
        except RestoreError as error:
            messages.error(request, f"{error.message} {error.hint}")
        return redirect("servers:detail", pk=snapshot.instance.server_id)


class SnapshotDeleteView(AdminRequiredMixin, View):
    """Delete a snapshot from the panel; the server is not touched."""

    def post(self, request: HttpRequest, snapshot_pk: int) -> HttpResponse:
        snapshot = get_object_or_404(
            ProtocolSnapshot.objects.select_related("instance"), pk=snapshot_pk
        )
        delete_snapshot(snapshot, self.member, self.client_ip)
        messages.success(request, _("The snapshot is deleted."))
        return redirect("servers:detail", pk=snapshot.instance.server_id)
