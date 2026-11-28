from typing import Any

from django.conf import settings
from django.contrib import messages
from django.db.models import QuerySet
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import FormView

from apps.accounts.permissions import AdminRequiredMixin, SectionRequiredMixin
from apps.accounts.services.access import Section
from apps.backups.errors import PanelArchiveError, RestoreError
from apps.backups.forms import PanelArchiveForm, PanelRestoreForm
from apps.backups.models import ProtocolSnapshot
from apps.backups.services.archive_remote import remote_from_settings
from apps.backups.services.archive_schedule import ArchiveSchedule
from apps.backups.services.panel_archives import (
    ArchiveFile,
    ArchiveNotFoundError,
    PanelArchiveFolder,
    active_archive_operation,
    delete_archive,
    failed_archive_operation,
    log_download,
    start_panel_archive,
)
from apps.backups.services.panel_restore import (
    RestoreSource,
    active_restore_operation,
    start_panel_restore,
)
from apps.backups.services.restore import start_restore, start_service_restore
from apps.backups.services.snapshot_restore import delete_snapshot, start_snapshot_restore
from apps.protocols.models import ProtocolInstance, ServiceInstance


class ProtocolRestoreView(SectionRequiredMixin, View):
    """Start restoring the protocol on its server; the server card shows the progress."""

    section = Section.BACKUPS

    def post(self, request: HttpRequest, instance_pk: int) -> HttpResponse:
        instances = self.zone.narrow(ProtocolInstance.objects.all(), server="server")
        instance = get_object_or_404(instances, pk=instance_pk)
        start_restore(instance, self.member, self.client_ip)
        return redirect("servers:detail", pk=instance.server_id)


class ServiceRestoreView(SectionRequiredMixin, View):
    """Start restoring the service on its server from the saved copy."""

    section = Section.BACKUPS

    def post(self, request: HttpRequest, service_pk: int) -> HttpResponse:
        services = self.zone.narrow(ServiceInstance.objects.all(), server="server")
        service = get_object_or_404(services, pk=service_pk)
        start_service_restore(service, self.member, self.client_ip)
        return redirect("servers:detail", pk=service.server_id)


class _SnapshotView(SectionRequiredMixin, View):
    section = Section.BACKUPS

    def snapshots(self) -> QuerySet[ProtocolSnapshot]:
        snapshots = ProtocolSnapshot.objects.select_related("instance")
        return self.zone.narrow(snapshots, server="instance__server")


class SnapshotRestoreView(_SnapshotView):
    """Start putting the files of a snapshot back on its server; refused while in a group."""

    def post(self, request: HttpRequest, snapshot_pk: int) -> HttpResponse:
        snapshot = get_object_or_404(self.snapshots(), pk=snapshot_pk)
        try:
            start_snapshot_restore(snapshot, self.member, self.client_ip)
        except RestoreError as error:
            messages.error(request, f"{error.message} {error.hint}")
        return redirect("servers:detail", pk=snapshot.instance.server_id)


class SnapshotDeleteView(_SnapshotView):
    """Delete a snapshot from the panel; the server is not touched."""

    def post(self, request: HttpRequest, snapshot_pk: int) -> HttpResponse:
        snapshot = get_object_or_404(self.snapshots(), pk=snapshot_pk)
        delete_snapshot(snapshot, self.member, self.client_ip)
        messages.success(request, _("The snapshot is deleted."))
        return redirect("servers:detail", pk=snapshot.instance.server_id)


class PanelArchiveListView(AdminRequiredMixin, FormView):  # type: ignore[type-arg]
    """Archives of the whole panel kept on it, and the form to make a new one."""

    template_name = "backups/panel_archives.html"
    form_class = PanelArchiveForm

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        archives = PanelArchiveFolder.from_settings().archives()
        kwargs.setdefault("form", PanelArchiveForm())
        kwargs.setdefault("restore_form", _restore_form(archives))
        return super().get_context_data(
            archives=archives,
            active=active_archive_operation(),
            restoring=active_restore_operation(),
            keep=settings.PANEL_ARCHIVE_KEEP,
            upload_mb=settings.PANEL_ARCHIVE_UPLOAD_MB,
            failed=failed_archive_operation(),
            schedule=ArchiveSchedule.from_settings(),
            storage=_storage_label(),
            **kwargs,
        )

    def form_valid(self, form: PanelArchiveForm) -> HttpResponse:
        operation = start_panel_archive(form.cleaned_data["password"], self.member, self.client_ip)
        return redirect("operations:detail", pk=operation.pk)


class PanelRestoreView(PanelArchiveListView):
    """Restore the panel from an uploaded archive or from one kept on it."""

    form_class = PanelRestoreForm  # type: ignore[assignment]

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return redirect("backups:panel-archives")

    def get_form(self, form_class: Any = None) -> PanelRestoreForm:
        archives = PanelArchiveFolder.from_settings().archives()
        return _restore_form(archives, data=self.request.POST, files=self.request.FILES)

    def form_invalid(self, form: PanelRestoreForm) -> HttpResponse:
        return self.render_to_response(self.get_context_data(restore_form=form))

    def form_valid(self, form: PanelRestoreForm) -> HttpResponse:  # type: ignore[override]
        upload = form.cleaned_data["upload"]
        if upload:
            name = PanelArchiveFolder.from_settings().upload(upload.chunks())
        else:
            name = form.cleaned_data["archive"]
        password = form.cleaned_data["restore_password"]
        operation = start_panel_restore(
            name,
            password,
            self.member,
            self.client_ip,
            source=RestoreSource.PAGE,
            label=upload.name if upload else name,
        )
        return redirect("operations:detail", pk=operation.pk)


def _restore_form(archives: list[ArchiveFile], **kwargs: Any) -> PanelRestoreForm:
    return PanelRestoreForm(archives=[archive.name for archive in archives], **kwargs)


def _storage_label() -> str | None:
    """Where copies go; None when nowhere, and an empty string when the settings are wrong."""
    try:
        remote = remote_from_settings()
    except PanelArchiveError:
        return ""
    return remote.label if remote is not None else None


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
