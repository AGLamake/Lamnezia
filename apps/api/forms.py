"""The forms of a new API token: its name and expiry, and the rows of its permissions."""

from datetime import datetime
from typing import Any, ClassVar

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import User
from apps.accounts.services.zone import Zone
from apps.api.models import Action, Resource
from apps.api.services.permissions import PermissionRuleError, check_rule
from apps.api.services.tokens import Permission
from apps.keys.models import GONE_STATUSES, AccessKey
from apps.keys.services.zone import KeyZone
from apps.servers.models import Server


class TokenForm(forms.Form):
    name = forms.CharField(
        label=_("Name"),
        max_length=100,
        help_text=_("What uses the token, e.g. monitoring or billing."),
    )
    expires_at = forms.DateTimeField(
        label=_("Expires at"),
        required=False,
        help_text=_("Empty: the token works until it is disabled."),
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )

    error_messages: ClassVar = {"past": _("The time is already past.")}

    def clean_expires_at(self) -> datetime | None:
        expires_at: datetime | None = self.cleaned_data["expires_at"]
        if expires_at is not None and expires_at <= timezone.now():
            raise forms.ValidationError(self.error_messages["past"], code="past")
        return expires_at


class _KeyChoice(forms.ModelChoiceField):  # type: ignore[type-arg]
    def label_from_instance(self, obj: Any) -> str:
        owner = obj.user.get_username() if obj.user else "—"
        return f"{obj.name} · {owner} · #{obj.pk}"


class PermissionForm(forms.Form):
    """One row of the permission builder: only objects of the member's zone are offered.

    An empty row is skipped; a row without an object covers the whole zone.
    """

    resource = forms.ChoiceField(
        label=_("Resource"), required=False, choices=[("", "—"), *Resource.choices]
    )
    action = forms.ChoiceField(label=_("Action"), choices=Action.choices, initial=Action.READ)
    server = forms.ModelChoiceField(
        label=_("Only the server"), required=False, queryset=Server.objects.none()
    )
    user = forms.ModelChoiceField(
        label=_("Only the user"), required=False, queryset=User.objects.none()
    )
    key = _KeyChoice(label=_("Only the key"), required=False, queryset=AccessKey.objects.none())

    error_messages: ClassVar = {
        "action": _("This resource can only be read."),
        "binding": _("This resource cannot be limited to such an object."),
        "objects": _("Choose one object at most."),
    }

    def __init__(self, *args: Any, member: User, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        zone = Zone(member)
        keys = KeyZone(member).keys().exclude(status__in=GONE_STATUSES).select_related("user")
        self._set_queryset("server", zone.servers().order_by("name"))
        self._set_queryset("user", zone.users().order_by("username"))
        self._set_queryset("key", keys.order_by("name", "pk"))

    def _set_queryset(self, name: str, queryset: Any) -> None:
        field = self.fields[name]
        assert isinstance(field, forms.ModelChoiceField)
        field.queryset = queryset

    def clean(self) -> dict[str, Any] | None:
        data = super().clean() or {}
        resource = data.get("resource")
        if not resource:
            return data
        bound = [name for name in ("server", "user", "key") if data.get(name) is not None]
        if len(bound) > 1:
            raise forms.ValidationError(self.error_messages["objects"], code="objects")
        try:
            check_rule(resource, data.get("action", Action.READ), bound[0] if bound else None)
        except PermissionRuleError as error:
            code = str(error)
            raise forms.ValidationError(self.error_messages[code], code=code) from None
        return data

    def permission(self) -> Permission | None:
        data = self.cleaned_data
        if not data.get("resource"):
            return None
        return Permission(
            resource=data["resource"],
            action=data["action"],
            server=data.get("server"),
            user=data.get("user"),
            key=data.get("key"),
        )


class BasePermissionFormSet(forms.BaseFormSet):  # type: ignore[type-arg]
    def clean(self) -> None:
        super().clean()
        if not any(self.errors) and not self.permissions():
            raise forms.ValidationError(_("Give the token at least one permission."), code="none")

    def permissions(self) -> list[Permission]:
        found = (form.permission() for form in self.forms if form.is_valid())
        return [permission for permission in found if permission is not None]


PermissionFormSet = forms.formset_factory(
    PermissionForm, formset=BasePermissionFormSet, extra=1, max_num=40, validate_max=True
)
