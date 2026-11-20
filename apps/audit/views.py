from typing import Any

from django.db.models import QuerySet
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
        return self.filter_form.filter(self.events())

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(filter_form=self.filter_form, **kwargs)

    def events(self) -> QuerySet[AuditEvent]:
        """Outside the whole zone, the actions of the member and their moderators only."""
        events = AuditEvent.objects.all()
        if self.zone.is_whole:
            return events
        return events.filter(actor__in=self.zone.team())
