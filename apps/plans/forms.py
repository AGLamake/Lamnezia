from decimal import Decimal
from typing import Any, ClassVar

from django import forms
from django.db.models import Model, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounts.services.zone import Zone
from apps.plans.models import Plan, Protocol, UserLimits

GIGABYTE = 1024**3
_CURRENCY_CODE_LENGTH = 3  # ISO 4217


class GigabytesField(forms.DecimalField):
    """Traffic typed in gigabytes and stored in bytes; empty means no limit."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("required", False)
        kwargs.setdefault("min_value", Decimal(0))
        kwargs.setdefault("decimal_places", 3)
        kwargs.setdefault("help_text", _("In GB. Empty: no limit."))
        super().__init__(**kwargs)

    def prepare_value(self, value: Any) -> Any:
        if isinstance(value, int):
            # Plain notation: normalize() alone turns 100 into 1E+2, which inputs reject.
            return f"{(Decimal(value) / GIGABYTE).normalize():f}"
        return value

    def clean(self, value: Any) -> int | None:
        gigabytes = super().clean(value)
        return None if gigabytes is None else int(gigabytes * GIGABYTE)


class PlanForm(forms.ModelForm):  # type: ignore[type-arg]
    """A plan on the servers and groups of the member's zone. A member who does not answer for
    the whole panel must choose servers, since none chosen means all of them; servers and groups
    of the plan outside the zone stay in it untouched."""

    traffic_limit = GigabytesField(label=_("Traffic per period"))
    total_traffic_limit = GigabytesField(label=_("Traffic in total"))
    protocols = forms.MultipleChoiceField(
        label=_("Protocols"),
        choices=Protocol.choices,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_("None selected: all protocols."),
    )

    class Meta:
        model = Plan
        fields = (
            "name",
            "traffic_limit",
            "period_days",
            "total_traffic_limit",
            "max_keys",
            "max_devices",
            "speed_limit",
            "access_days",
            "servers",
            "groups",
            "protocols",
            "is_trial",
            "price",
            "currency",
        )
        widgets: ClassVar = {
            "servers": forms.CheckboxSelectMultiple,
            "groups": forms.CheckboxSelectMultiple,
        }
        help_texts: ClassVar = {
            "max_keys": _("Empty: no limit."),
            "max_devices": _(
                "Devices that fetch the subscription link with x-hwid. Empty: no limit."
            ),
            "speed_limit": _("Per key, each way. Empty: no limit."),
            "price": _("With a payment plugin, users can buy the plan at this price."),
        }

    def __init__(self, *args: Any, zone: Zone, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._zone = zone
        # A server of a group is chosen with its group: its key is the key of the group.
        servers = self.fields["servers"]
        servers.queryset = (  # type: ignore[attr-defined]
            zone.servers().exclude(instances__group_member__isnull=False).distinct()
        )
        self.fields["groups"].queryset = zone.groups()  # type: ignore[attr-defined]
        if not zone.is_whole:
            servers.required = True
            servers.help_text = _("The plan works on the servers chosen here.")

    def clean_servers(self) -> list[Model]:
        return self._with_kept("servers", self._zone.servers())

    def clean_groups(self) -> list[Model]:
        return self._with_kept("groups", self._zone.groups())

    def _with_kept(self, field: str, mine: QuerySet[Any]) -> list[Model]:
        chosen: list[Model] = list(self.cleaned_data[field])
        if self.instance.pk is None or self._zone.is_whole:
            return chosen
        kept = getattr(self.instance, field).exclude(pk__in=mine)
        return [*chosen, *kept]

    def clean_currency(self) -> str:
        currency: str = self.cleaned_data["currency"].strip().upper()
        if currency and not (len(currency) == _CURRENCY_CODE_LENGTH and currency.isalpha()):
            raise forms.ValidationError(_("Use a three-letter code, e.g. EUR or RUB."))
        return currency


class UserLimitsForm(forms.ModelForm):  # type: ignore[type-arg]
    """The user's plan and personal overrides; an empty override takes the plan's value. The
    plans offered are the member's own and the user's current one."""

    traffic_limit = GigabytesField(
        label=_("Traffic per period"), help_text=_("In GB. Empty: as in the plan.")
    )
    total_traffic_limit = GigabytesField(
        label=_("Traffic in total"), help_text=_("In GB. Empty: as in the plan.")
    )

    class Meta:
        model = UserLimits
        fields = (
            "plan",
            "traffic_limit",
            "period_days",
            "total_traffic_limit",
            "max_keys",
            "max_devices",
            "speed_limit",
            "access_until",
        )
        widgets: ClassVar = {
            "access_until": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            )
        }
        help_texts: ClassVar = {
            "plan": _("A new plan starts the access term again."),
            "period_days": _("Empty: as in the plan."),
            "max_keys": _("Empty: as in the plan."),
            "max_devices": _("Empty: as in the plan."),
            "speed_limit": _("Per key, each way. Empty: as in the plan."),
            "access_until": _("Empty: the term of the plan."),
        }

    def __init__(self, *args: Any, zone: Zone, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        current = Plan.objects.filter(pk=self.instance.plan_id)
        self.fields["plan"].queryset = (zone.offered_plans() | current).distinct()  # type: ignore[attr-defined]


class UserLookupForm(forms.Form):
    username = forms.CharField(label=_("User name"), max_length=150)
