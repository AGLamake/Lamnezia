"""Filter of the operations list: status and a search by the subject, kept in the URL."""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.operations.models import OperationQuerySet, OperationStatus


class OperationFilterForm(forms.Form):
    q = forms.CharField(label=_("Search"), required=False)
    status = forms.ChoiceField(
        label=_("Status"), required=False, choices=[("", _("Any")), *OperationStatus.choices]
    )

    def filter(self, operations: OperationQuerySet) -> OperationQuerySet:
        if not self.is_valid():
            return operations
        data = self.cleaned_data
        if data["q"]:
            operations = operations.filter(subject__icontains=data["q"])
        if data["status"]:
            operations = operations.filter(status=data["status"])
        return operations
