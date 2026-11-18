"""The form of a new API token: its name, the staff member it acts as, scope and expiry."""

from datetime import datetime
from typing import ClassVar

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import STAFF_ROLES, Status, User
from apps.api.models import TokenScope


class TokenForm(forms.Form):
    name = forms.CharField(
        label=_("Name"),
        max_length=100,
        help_text=_("What uses the token, e.g. monitoring or billing."),
    )
    owner = forms.ModelChoiceField(
        label=_("Acts as"),
        queryset=User.objects.none(),
        help_text=_("The token sees what this staff member sees: a moderator's token, their zone."),
    )
    scope = forms.ChoiceField(label=_("Scope"), choices=TokenScope.choices, initial=TokenScope.READ)
    expires_at = forms.DateTimeField(
        label=_("Expires at"),
        required=False,
        help_text=_("Empty: the token works until it is disabled."),
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )

    error_messages: ClassVar = {"past": _("The time is already past.")}

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        staff = User.objects.filter(role__in=STAFF_ROLES, is_active=True, status=Status.ACTIVE)
        owner = self.fields["owner"]
        assert isinstance(owner, forms.ModelChoiceField)
        owner.queryset = staff.order_by("username")

    def clean_expires_at(self) -> datetime | None:
        expires_at: datetime | None = self.cleaned_data["expires_at"]
        if expires_at is not None and expires_at <= timezone.now():
            raise forms.ValidationError(self.error_messages["past"], code="past")
        return expires_at
