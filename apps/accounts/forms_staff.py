"""Forms of the «Staff» section: a new member, their rights and links, a role, a member to add.

Every choice comes from `StaffDesk`, so a server administrator is offered only their own servers,
plans and moderators, and rights no higher than their own; anything else in a POST fails
validation.
"""

from typing import Any

from django import forms
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounts.forms import RegistrationForm
from apps.accounts.models import User
from apps.accounts.services.access import Access, Level, Section
from apps.accounts.services.staff import StaffDesk
from apps.plans.models import Plan
from apps.servers.models import Server

LEVEL_LABELS = {
    Level.NONE: _("No access"),
    Level.VIEW: _("View"),
    Level.MANAGE: _("Manage"),
}

_ACCESS_PREFIX = "access_"


def _role_choices(desk: StaffDesk) -> list[tuple[str, str]]:
    return [(role.value, str(role.label)) for role in desk.roles()]


class StaffCreateForm(RegistrationForm):
    """Account data and the role of a new staff member; `StaffDesk.create` makes the account."""

    role = forms.ChoiceField(label=_("Role"))
    must_change_password = forms.BooleanField(
        label=_("Change the password at the next sign-in"),
        required=False,
        initial=True,
        help_text=_("Until then the panel opens only the page to choose a new password."),
    )

    class Meta(RegistrationForm.Meta):
        fields = ("username", "email")

    def __init__(self, *args: Any, desk: StaffDesk, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = _role_choices(desk)  # type: ignore[attr-defined]


class StaffRoleForm(forms.Form):
    role = forms.ChoiceField(label=_("Role"))

    def __init__(self, *args: Any, desk: StaffDesk, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = _role_choices(desk)  # type: ignore[attr-defined]


class StaffForm(forms.Form):
    """Quotas, servers, plans and a level per section of the panel."""

    user_quota = forms.IntegerField(
        label=_("users at most"),
        min_value=0,
        required=False,
        help_text=_("How many users the member may serve. Empty: no limit."),
    )
    key_quota = forms.IntegerField(
        label=_("keys at most"),
        min_value=0,
        required=False,
        help_text=_("How many keys the member's users may hold. Empty: no limit."),
    )
    servers = forms.ModelMultipleChoiceField(
        label=_("Servers"),
        queryset=Server.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    plans = forms.ModelMultipleChoiceField(
        label=_("Plans"),
        queryset=Plan.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Users of these plans are linked to the member."),
    )

    def __init__(self, *args: Any, desk: StaffDesk, target: User, **kwargs: Any) -> None:
        mine_servers, mine_plans = desk.zone.servers(), desk.zone.plans()
        target_access = Access(target)
        kwargs.setdefault(
            "initial",
            {
                "user_quota": target.user_quota,
                "key_quota": target.key_quota,
                "servers": target.moderated_servers.filter(pk__in=mine_servers),
                "plans": target.staffed_plans.filter(pk__in=mine_plans),
                **{
                    _ACCESS_PREFIX + section: target_access.level(section).value
                    for section in Section.values
                },
            },
        )
        super().__init__(*args, **kwargs)
        self._target = target
        self._set_queryset("servers", mine_servers.order_by("name"))
        self._set_queryset("plans", mine_plans.order_by("name"))
        for section in Section:
            levels = desk.levels(target, section)
            if len(levels) > 1:
                self.fields[_ACCESS_PREFIX + section] = forms.TypedChoiceField(
                    label=section.label,
                    choices=[(level.value, LEVEL_LABELS[level]) for level in levels],
                    coerce=int,
                    required=False,
                    empty_value=None,
                )

    def clean_user_quota(self) -> int | None:
        return self._quota("user_quota")

    def clean_key_quota(self) -> int | None:
        return self._quota("key_quota")

    def _quota(self, name: str) -> int | None:
        """A moderator's quota, no higher than the one of the server administrator who made them."""
        value: int | None = self.cleaned_data[name]
        if error := self._target.quota_error(name, value):
            raise forms.ValidationError(error)
        return value

    def _set_queryset(self, name: str, queryset: QuerySet[Any]) -> None:
        self.fields[name].queryset = queryset  # type: ignore[attr-defined]

    def access_fields(self) -> list[forms.BoundField]:
        return [self[name] for name in self.fields if name.startswith(_ACCESS_PREFIX)]

    def levels(self) -> dict[str, Level]:
        """The chosen level of every section offered in the form; a section left out keeps its."""
        return {
            name.removeprefix(_ACCESS_PREFIX): Level(value)
            for name, value in self.cleaned_data.items()
            if name.startswith(_ACCESS_PREFIX) and value is not None
        }


class StaffPickForm(forms.Form):
    """A member to add to a server, a plan or a user: one of `candidates`."""

    member = forms.ModelChoiceField(label=_("Staff member"), queryset=User.objects.none())

    def __init__(self, *args: Any, candidates: QuerySet[User], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["member"].queryset = candidates  # type: ignore[attr-defined]
