from typing import Any

from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import FormView

from apps.accounts.permissions import AdminRequiredMixin, SectionRequiredMixin
from apps.accounts.services.access import Section
from apps.backups.errors import RestoreError
from apps.backups.forms import PanelArchiveForm
from apps.backups.models import ProtocolSnapshot
from apps.backups.services.panel_archives import (
    ArchiveNotFoundError,
    PanelArchiveFolder,
    active_archive_operation,
    delete_archive,
    log_download,
    start_panel_archive,
)
from apps.backups.services.restore import start_restore, start_service_restore
from apps.backups.services.snapshot_restore import delete_snapshot, start_snapshot_restore
from apps.protocols.models import ProtocolInstance, ServiceInstance


class ProtocolRestoreView(SectionRequiredMixin, View):
    """Start restoring the protocol on its server; the server card shows the progress."""

    section = Section.BACKUPS

    def post(self, request: HttpRequest, instance_pk: int) -> HttpResponse:
        instance = get_object_or_404(ProtocolInstance, pk=instance_pk)
        start_restore(instance, self.member, self.client_ip)
        return redirect("servers:detail", pk=instance.server_id)


class ServiceRestoreView(SectionRequiredMixin, View):
    """Start restoring the service on its server from the saved copy."""

    section = Section.BACKUPS

    def post(self, request: HttpRequest, service_pk: int) -> HttpResponse:
        service = get_object_or_404(ServiceInstance, pk=service_pk)
        start_service_restore(service, self.member, self.client_ip)
        return redirect("servers:detail", pk=service.server_id)


class SnapshotRestoreView(SectionRequiredMixin, View):
    """Start putting the files of a snapshot back on its server; refused while in a group."""

    section = Section.BACKUPS

    def post(self, request: HttpRequest, snapshot_pk: int) -> HttpResponse:
        snapshot = get_object_or_404(
            ProtocolSnapshot.objects.select_related("instance"), pk=snapshot_pk
        )
        try:
            start_snapshot_restore(snapshot, self.member, self.client_ip)
        except RestoreError as error:
            messages.error(request, f"{error.message} {error.hint}")
        return redirect("servers:detail", pk=snapshot.instance.server_id)


class SnapshotDeleteView(SectionRequiredMixin, View):
    """Delete a snapshot from the panel; the server is not touched."""

    section = Section.BACKUPS

    def post(self, request: HttpRequest, snapshot_pk: int) -> HttpResponse:
        snapshot = get_object_or_404(
            ProtocolSnapshot.objects.select_related("instance"), pk=snapshot_pk
        )
        delete_snapshot(snapshot, self.member, self.client_ip)
        messages.success(request, _("The snapshot is deleted."))
        return redirect("servers:detail", pk=snapshot.instance.server_id)


class PanelArchiveListView(AdminRequiredMixin, FormView):  # type: ignore[type-arg]
    """Archives of the whole panel kept on it, and the form to make a new one."""

    template_name = "backups/panel_archives.html"
    form_class = PanelArchiveForm

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        folder = PanelArchiveFolder.from_settings()
        return super().get_context_data(
            archives=folder.archives(),
            active=active_archive_operation(),
            keep=settings.PANEL_ARCHIVE_KEEP,
            **kwargs,
        )

    def form_valid(self, form: PanelArchiveForm) -> HttpResponse:
        operation = start_panel_archive(form.cleaned_data["password"], self.member, self.client_ip)
        return redirect("operations:detail", pk=operation.pk)


class PanelArchiveDownloadView(AdminRequiredMixin, View):
    def get(self, request: HttpRequest, name: str) -> FileResponse:
        try:
            archive = PanelArchiveFolder.from_settings().get(name)
        except ArchiveNotFoundError as exc:
            raise Http404 from exc
        log_download(archive, self.member, self.client_ip)
        return FileResponse(archive.path.open("rb"), as_attachment=True, filename=archive.name)


class PanelArchiveDeleteView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, name: str) -> HttpResponse:
        try:
            delete_archive(PanelArchiveFolder.from_settings(), name, self.member, self.client_ip)
        except ArchiveNotFoundError as exc:
            raise Http404 from exc
        messages.success(request, _("The archive is deleted."))
        return redirect("backups:panel-archives")
