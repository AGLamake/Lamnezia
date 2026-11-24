from typing import Any, cast

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.guides.services.devices import Device
from apps.keys.services.instances import Instance
from apps.portal.services.speed_test import MAX_PORTS


class GetKeyForm(forms.Form):
    """Getting a key: the device first, then the server (preset when it is the only one) and an
    optional name. An empty name is the device's.
    """

    device = forms.ChoiceField(label=_("Device"), choices=Device.choices, widget=forms.RadioSelect)
    source = forms.ChoiceField(label=_("Server"), widget=forms.RadioSelect)
    name = forms.CharField(
        label=_("Key name"),
        max_length=100,
        required=False,
        help_text=_("To tell your devices apart, for example «Anna's phone»."),
    )

    def __init__(self, *args: Any, instances: list[Instance], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        source = cast(forms.ChoiceField, self.fields["source"])
        source.choices = [(instance.source, instance.title) for instance in instances]
        if len(instances) == 1:
            source.initial = instances[0].source

    def clean_name(self) -> str:
        return cast(str, self.cleaned_data["name"]).strip()

    def key_name(self) -> str:
        return self.cleaned_data["name"] or Device(self.cleaned_data["device"]).label


class RenameKeyForm(forms.Form):
    name = forms.CharField(label=_("Key name"), max_length=100)

    def clean_name(self) -> str:
        name = cast(str, self.cleaned_data["name"]).strip()
        if not name:
            raise forms.ValidationError(_("Enter a name."))
        return name


class SpeedTestForm(forms.Form):
    """Which of the allowed free ports of the server to measure."""

    ports = forms.TypedMultipleChoiceField(
        label=_("Ports"),
        coerce=int,
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Up to four ports. Compare a port up to 9999 with a higher one."),
    )

    def __init__(self, *args: Any, ports: tuple[int, ...], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        field = cast(forms.TypedMultipleChoiceField, self.fields["ports"])
        field.choices = [(port, str(port)) for port in ports]
        field.initial = list(ports[:MAX_PORTS])

    def clean_ports(self) -> tuple[int, ...]:
        ports = tuple(self.cleaned_data["ports"])
        if len(ports) > MAX_PORTS:
            raise forms.ValidationError(_("Choose up to four ports."))
        return ports
