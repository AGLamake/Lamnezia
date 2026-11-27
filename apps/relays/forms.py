"""Forms of the relay: the settings of the relay card, and the mode, sources and own entries of
the network list of a split group."""

from urllib.parse import urlsplit

from django import forms
from django.utils.translation import gettext_lazy as _

from amnezia_core.split_lists import SplitListError, Target
from apps.groups.models import ServerGroup
from apps.relays.models import Relay, SplitEntry, SplitSource


class RelaySettingsForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = Relay
        fields = ("check_interval", "failures")


class SplitListModeForm(forms.ModelForm):  # type: ignore[type-arg]
    """The mode of the network list; the view changes it through the service, not `save`."""

    class Meta:
        model = ServerGroup
        fields = ("list_mode",)


class SplitSourceForm(forms.ModelForm):  # type: ignore[type-arg]
    """A list the panel downloads every day: by HTTP or HTTPS, once per group."""

    class Meta:
        model = SplitSource
        fields = ("url", "kind")

    def __init__(self, *args: object, group: ServerGroup, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.instance.group = group

    def clean_url(self) -> str:
        url: str = self.cleaned_data["url"]
        if urlsplit(url).scheme not in {"http", "https"}:
            raise forms.ValidationError(_("The list is downloaded by HTTP or HTTPS."))
        if self.instance.group.split_sources.filter(url=url).exists():
            raise forms.ValidationError(_("The group already has this list."))
        return url


class SplitEntryForm(forms.ModelForm):  # type: ignore[type-arg]
    """An own network or domain of the group, kept in the form the agent gets it."""

    class Meta:
        model = SplitEntry
        fields = ("value", "direction")

    def __init__(self, *args: object, group: ServerGroup, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.instance.group = group

    def clean_value(self) -> str:
        typed: str = self.cleaned_data["value"]
        if ":" in typed:
            raise forms.ValidationError(_("Only IPv4 networks are routed."))
        try:
            value = str(Target.parse(typed))
        except SplitListError:
            raise forms.ValidationError(_("Enter an IPv4 network or a domain name.")) from None
        if self.instance.group.split_entries.filter(value=value).exists():
            raise forms.ValidationError(_("The group already has this entry."))
        return value
