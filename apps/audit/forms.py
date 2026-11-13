from typing import Any, cast

from django import forms
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.audit.models import AuditEvent
from apps.audit.services.actions import action_label


class AuditFilterForm(forms.Form):
    action = forms.ChoiceField(label=_("Action"), required=False)
    actor = forms.CharField(label=_("Who"), required=False)
    target = forms.CharField(label=_("Object"), required=False)
    since = forms.DateField(
        label=_("From"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    until = forms.DateField(
        label=_("To"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        actions = AuditEvent.objects.order_by("action").values_list("action", flat=True).distinct()
        choices = [("", _("Any")), *((action, action_label(action)) for action in actions)]
        cast(forms.ChoiceField, self.fields["action"]).choices = choices

    def filter(self, events: QuerySet[AuditEvent]) -> QuerySet[AuditEvent]:
        if not self.is_valid():
            return events
        data = self.cleaned_data
        if data["action"]:
            events = events.filter(action=data["action"])
        if data["actor"]:
            events = events.filter(actor_name__icontains=data["actor"])
        if data["target"]:
            events = events.filter(target_repr__icontains=data["target"])
        if data["since"]:
            events = events.filter(created_at__date__gte=data["since"])
        if data["until"]:
            events = events.filter(created_at__date__lte=data["until"])
        return events
