from typing import Any

from django.views.generic import ListView

from apps.accounts.permissions import SectionRequiredMixin
from apps.accounts.services.access import Section
from apps.audit.forms import AuditFilterForm
from apps.audit.models import AuditEvent
from apps.core.paging import PageSizeMixin


class AuditListView(PageSizeMixin, SectionRequiredMixin, ListView):  # type: ignore[type-arg]
    section = Section.AUDIT
    template_name = "audit/list.html"
    context_object_name = "events"

    def get_queryset(self) -> Any:
        self.filter_form = AuditFilterForm(self.request.GET or None)
        return self.filter_form.filter(AuditEvent.objects.all())

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(filter_form=self.filter_form, **kwargs)
