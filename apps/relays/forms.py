"""The settings form of the relay card: how often the agent probes the members and how many
failures switch a group."""

from django import forms

from apps.relays.models import Relay


class RelaySettingsForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = Relay
        fields = ("check_interval", "failures")
