from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from apps.accounts.permissions import AdminRequiredMixin
from apps.backups.services.restore import start_restore, start_service_restore
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
