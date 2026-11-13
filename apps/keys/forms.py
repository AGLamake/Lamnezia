from typing import Any, cast

from django import forms
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import User
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.zone import KeyZone


class KeyFilterForm(forms.Form):
    status = forms.ChoiceField(
        label=_("Status"), choices=[("", _("Any")), *KeyStatus.choices], required=False
    )
    source = forms.ChoiceField(label=_("Server"), required=False)
    owner = forms.CharField(label=_("Owner"), required=False)
    no_owner = forms.BooleanField(label=_("Without owner"), required=False)

    def __init__(self, *args: Any, zone: KeyZone, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        sources = set(zone.keys().values_list("source", flat=True).distinct())
        titles = zone.titles()
        choices = [(s, titles.get(s, s)) for s in sorted(sources | titles.keys())]
        cast(forms.ChoiceField, self.fields["source"]).choices = [("", _("Any")), *choices]

    def filter(self, keys: QuerySet[AccessKey]) -> QuerySet[AccessKey]:
        if not self.is_valid():
            return keys
        data = self.cleaned_data
        if data["status"]:
            keys = keys.filter(status=data["status"])
        if data["source"]:
            keys = keys.filter(source=data["source"])
        if data["owner"]:
            keys = keys.filter(user__username__icontains=data["owner"])
        if data["no_owner"]:
            keys = keys.filter(user=None)
        return keys


class KeyIssueForm(forms.Form):
    user = forms.ModelChoiceField(label=_("User"), queryset=User.objects.none())
    source = forms.ChoiceField(label=_("Server"))
    name = forms.CharField(label=_("Device name"), max_length=100)

    def __init__(self, *args: Any, zone: KeyZone, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._zone = zone
        self.fields["user"].queryset = zone.owners()  # type: ignore[attr-defined]
        cast(forms.ChoiceField, self.fields["source"]).choices = list(zone.issue_titles().items())

    def clean(self) -> dict[str, Any]:
        if self._zone.quota_reached():
            raise forms.ValidationError(_("Your users hold as many keys as your quota allows."))
        super().clean()
        return self.cleaned_data


class KeyAssignForm(forms.Form):
    user = forms.ModelChoiceField(label=_("User"), queryset=User.objects.none())

    def __init__(self, *args: Any, zone: KeyZone, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = zone.owners()  # type: ignore[attr-defined]


class KeyBulkForm(forms.Form):
    """A state change for the keys ticked in the list; keys outside the zone are not offered."""

    action = forms.ChoiceField(
        label=_("With the ticked keys"),
        choices=[("disable", _("Disable")), ("enable", _("Enable")), ("revoke", _("Revoke"))],
    )
    keys = forms.ModelMultipleChoiceField(queryset=AccessKey.objects.none())

    def __init__(self, *args: Any, zone: KeyZone, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["keys"].queryset = zone.keys()  # type: ignore[attr-defined]
