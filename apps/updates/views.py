"""Pages of the panel update: versions, the update button, the status and log from the host."""

from typing import Any

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.utils.functional import cached_property
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import FormView, TemplateView

from apps.accounts.permissions import AdminRequiredMixin
from apps.updates.errors import UpdateError, host_reason
from apps.updates.forms import PanelUpdateForm
from apps.updates.services.updates import PanelUpdate


class _UpdateContextMixin(AdminRequiredMixin):
    """What the page and its self-reloading status part both show."""

    @cached_property
    def update(self) -> PanelUpdate:
        return PanelUpdate.from_settings()

    def status_context(self) -> dict[str, Any]:
        host = self.update.host
        status = host.status()
        return {
            "status": status,
            "status_reason": host_reason(status.reason) if status and status.reason else "",
            "log": host.log(),
            "pending": host.pending(),
            "stuck_request": host.stuck_request(),
            "active": self.update.active_operation(),
            "busy": self.update.busy(),
            "poll_seconds": settings.PROGRESS_POLL_SECONDS,
        }


class PanelUpdateView(_UpdateContextMixin, FormView):  # type: ignore[type-arg]
    template_name = "updates/update.html"
    form_class = PanelUpdateForm

    def get_initial(self) -> dict[str, Any]:
        release = self.update.offer().release
        return {"version": str(release.version)} if release else {}

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        host = self.update.host
        return super().get_context_data(
            offer=self.update.offer(),
            check_enabled=self.update.releases.enabled,
            host_available=host.available,
            channel=host.channel(),
            **self.status_context(),
            **kwargs,
        )

    def form_valid(self, form: PanelUpdateForm) -> HttpResponse:
        data = form.cleaned_data
        try:
            self.update.start(data["version"], data["password"], self.member, self.client_ip)
        except UpdateError as exc:
            form.add_error(None, f"{exc.message} {exc.hint}")
            return self.form_invalid(form)
        messages.success(self.request, _("The update is started."))
        return redirect("updates:update")


class PanelUpdateStatusView(_UpdateContextMixin, TemplateView):
    """The status block alone, reloaded by the page while an update goes on."""

    template_name = "updates/_status.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(**self.status_context(), **kwargs)


class CheckReleaseView(AdminRequiredMixin, View):
    """Asks the worker to check GitHub now instead of waiting for the next scheduled check."""

    def post(self, request: HttpRequest) -> HttpResponse:
        PanelUpdate.from_settings().releases.request_check()
        messages.info(request, _("The check is queued: open the page again in a minute."))
        return redirect("updates:update")
