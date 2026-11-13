from typing import Any, cast

from django import forms
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import LinkedAccount, Messenger, Status, User
from apps.accounts.services.zone import Zone
from apps.dashboard.models import BatchAction
from apps.dashboard.services.stats import Period, Scope
from apps.dashboard.services.users import users_on_server
from apps.keys.models import AccessKey
from apps.keys.services.zone import KeyZone
from apps.plans.models import Plan
from apps.servers.models import Server

# Filter values for users without a plan and without a linked messenger account.
NO_PLAN = "none"
NOT_LINKED = "none"
_MAX_EXTENSION_DAYS = 3650


class ShowcaseForm(forms.Form):
    """Sample form for the /dev/ui component showcase."""

    name = forms.CharField(label=_("Name"), help_text=_("Shown to other users."))
    email = forms.EmailField(label=_("Email"))
    plan = forms.ChoiceField(
        label=_("Plan"), choices=[("trial", _("Trial")), ("friends", _("Friends"))]
    )
    note = forms.CharField(
        label=_("Note"), widget=forms.Textarea(attrs={"rows": 3}), required=False
    )
    agree = forms.BooleanField(label=_("I agree"))


class UserFilterForm(forms.Form):
    search = forms.CharField(
        label=_("Search"),
        required=False,
        widget=forms.TextInput(attrs={"placeholder": _("Name, email or messenger account")}),
    )
    plan = forms.ChoiceField(label=_("Plan"), required=False)
    status = forms.ChoiceField(
        label=_("Status"), choices=[("", _("Any")), *Status.choices], required=False
    )
    server = forms.ModelChoiceField(
        label=_("Server"),
        queryset=Server.objects.order_by("name"),
        required=False,
        empty_label=_("Any"),
        help_text=_("Users with a key on the server."),
    )
    messenger = forms.ChoiceField(
        label=_("Messenger"),
        choices=[("", _("Any")), *Messenger.choices, (NOT_LINKED, _("Not linked"))],
        required=False,
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        plans = [(str(plan.pk), plan.name) for plan in Plan.objects.all()]
        cast(forms.ChoiceField, self.fields["plan"]).choices = [
            ("", _("Any")),
            (NO_PLAN, _("No plan")),
            *plans,
        ]

    def filter(self, users: QuerySet[User]) -> QuerySet[User]:
        if not self.is_valid():
            return users
        data = self.cleaned_data
        if data["search"]:
            term = data["search"]
            accounts = LinkedAccount.objects.filter(
                Q(username__icontains=term.lstrip("@")) | Q(external_id=term)
            )
            users = users.filter(
                Q(username__icontains=term)
                | Q(email__icontains=term)
                | Q(pk__in=accounts.values("user_id"))
            )
        if data["plan"] == NO_PLAN:
            users = users.filter(Q(limits=None) | Q(limits__plan=None))
        elif data["plan"]:
            users = users.filter(limits__plan_id=data["plan"])
        if data["status"]:
            users = users.filter(status=data["status"])
        if data["server"]:
            users = users_on_server(users, data["server"])
        if data["messenger"] == NOT_LINKED:
            users = users.exclude(pk__in=LinkedAccount.objects.values("user_id"))
        elif data["messenger"]:
            linked = LinkedAccount.objects.filter(messenger=data["messenger"])
            users = users.filter(pk__in=linked.values("user_id"))
        return users


class LinkAccountForm(forms.Form):
    """A messenger account the administrator links to a user by hand."""

    external_id = forms.RegexField(
        label=_("Account ID"),
        regex=r"^\d{1,32}$",
        error_messages={"invalid": _("The account ID is a number.")},
    )
    username = forms.CharField(label=_("Name in the messenger"), max_length=100, required=False)

    def clean_username(self) -> str:
        name: str = self.cleaned_data["username"]
        return name.strip().lstrip("@")


class UserBatchForm(forms.Form):
    """A bulk action on the users ticked in the list; `users` are those it may touch."""

    users = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        error_messages={"required": _("Tick at least one user.")},
    )
    action = forms.ChoiceField(label=_("Action"), choices=BatchAction.choices)
    days = forms.IntegerField(
        label=_("Days"), min_value=1, max_value=_MAX_EXTENSION_DAYS, required=False
    )
    plan = forms.ModelChoiceField(label=_("Plan"), queryset=Plan.objects.all(), required=False)

    def __init__(self, *args: Any, users: QuerySet[User], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["users"].queryset = users  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        action = data.get("action")
        if action == BatchAction.EXTEND and not data.get("days"):
            self.add_error("days", _("How many days to add?"))
        if action == BatchAction.CHANGE_PLAN and not data.get("plan"):
            self.add_error("plan", _("Choose a plan."))
        return data


class StatsFilterForm(forms.Form):
    """Period and scope of the statistics page; the user and key come from links on their pages.

    The servers, users and keys to choose from are those of the staff member's zone.
    """

    period = forms.ChoiceField(label=_("Period"), choices=Period.choices, required=False)
    server = forms.ModelChoiceField(
        label=_("Server"),
        queryset=Server.objects.order_by("name"),
        required=False,
        empty_label=_("All servers"),
    )
    user = forms.ModelChoiceField(
        queryset=User.objects.all(), required=False, widget=forms.HiddenInput
    )
    key = forms.ModelChoiceField(
        queryset=AccessKey.objects.select_related("user"),
        required=False,
        widget=forms.HiddenInput,
    )

    def __init__(self, data: Any, *, member: User) -> None:
        # Always bound: a wrong value falls back to its default instead of an error page.
        super().__init__(data)
        zone = Zone(member)
        self._keys = KeyZone(member)
        servers = zone.narrow(Server.objects.order_by("name"), server="pk")
        self.fields["server"].queryset = servers  # type: ignore[attr-defined]
        self.fields["user"].queryset = zone.users()  # type: ignore[attr-defined]
        keys = self._keys.keys().select_related("user")
        self.fields["key"].queryset = keys  # type: ignore[attr-defined]
        self.is_valid()

    @property
    def selected_period(self) -> Period:
        return Period(self.cleaned_data.get("period") or Period.WEEK)

    @property
    def scope(self) -> Scope:
        data = self.cleaned_data
        return Scope(
            server=data.get("server"), user=data.get("user"), key=data.get("key"), zone=self._keys
        )
