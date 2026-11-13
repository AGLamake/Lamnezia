from typing import Any

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import DetailView, ListView

from apps.accounts.permissions import StaffRequiredMixin
from apps.core.paging import PageSizeMixin
from apps.operations.forms import OperationFilterForm
from apps.operations.models import Operation, OperationQuerySet, StepStatus
from apps.operations.services.launcher import OperationNotRetryableError, retry


class _OperationsView(StaffRequiredMixin):
    """An administrator sees every operation; a moderator only the ones they started."""

    def operations(self) -> OperationQuerySet:
        operations = Operation.objects.all()
        if self.member.is_admin:
            return operations
        return operations.filter(created_by=self.member)


class OperationListView(PageSizeMixin, _OperationsView, ListView):  # type: ignore[type-arg]
    """Newest first; while an operation of the page runs, the table reloads itself over HTMX."""

    template_name = "operations/list.html"
    context_object_name = "operations"

    def get_queryset(self) -> Any:
        self.filter_form = OperationFilterForm(self.request.GET or None)
        return self.filter_form.filter(self.operations().newest_first())

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(filter_form=self.filter_form, **kwargs)
        context["running"] = any(not operation.is_finished for operation in context["operations"])
        context["poll_seconds"] = settings.PROGRESS_POLL_SECONDS
        return context


class OperationDetailView(_OperationsView, DetailView):  # type: ignore[type-arg]
    """Operation page; the progress block reloads itself over HTMX until the operation ends."""

    template_name = "operations/detail.html"
    context_object_name = "operation"

    def get_queryset(self) -> OperationQuerySet:
        return self.operations()

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(**progress_context(self.object), **kwargs)


def progress_context(operation: Operation) -> dict[str, Any]:
    """Context of `operations/_progress.html`, for pages that embed an operation's progress."""
    steps = list(operation.steps.all())
    return {
        "operation": operation,
        "steps": steps,
        "failed_step": next((s for s in steps if s.status == StepStatus.FAILED), None),
        "retry": operation.retries.first(),
        "poll_seconds": settings.PROGRESS_POLL_SECONDS,
    }


class OperationProgressView(OperationDetailView):
    template_name = "operations/_progress.html"


class OperationRetryView(_OperationsView, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        operation = get_object_or_404(self.operations(), pk=pk)
        try:
            operation = retry(operation, user=self.member)
        except OperationNotRetryableError:
            messages.error(request, _("Only a failed operation can be retried."))
        return redirect("operations:detail", pk=operation.pk)
