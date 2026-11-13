from decimal import Decimal
from typing import Any, ClassVar

from django import forms
from django.utils.translation import gettext_lazy as _

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
            "speed_limit",
            "access_days",
            "servers",
            "protocols",
            "is_trial",
            "price",
            "currency",
        )
        widgets: ClassVar = {"servers": forms.CheckboxSelectMultiple}
        help_texts: ClassVar = {
            "max_keys": _("Empty: no limit."),
            "speed_limit": _("Per key, each way. Empty: no limit."),
            "price": _("With a payment plugin, users can buy the plan at this price."),
        }

    def clean_currency(self) -> str:
        currency: str = self.cleaned_data["currency"].strip().upper()
        if currency and not (len(currency) == _CURRENCY_CODE_LENGTH and currency.isalpha()):
            raise forms.ValidationError(_("Use a three-letter code, e.g. EUR or RUB."))
        return currency


class UserLimitsForm(forms.ModelForm):  # type: ignore[type-arg]
    """The user's plan and personal overrides; an empty override takes the plan's value."""

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
            "speed_limit": _("Per key, each way. Empty: as in the plan."),
            "access_until": _("Empty: the term of the plan."),
        }


class UserLookupForm(forms.Form):
    username = forms.CharField(label=_("User name"), max_length=150)
