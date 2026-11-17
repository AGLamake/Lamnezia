"""Form of a panel move: the new machine, chosen from the servers of the panel."""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.relocation.services.panel_machine import PanelMachine
from apps.servers.models import Server


class MoveForm(forms.Form):
    target = forms.ModelChoiceField(
        label=_("New machine"),
        queryset=Server.objects.order_by("name"),
        help_text=_(
            "Add the machine as a server first: the move uses its SSH access. "
            "It must run Debian or Ubuntu and have no panel yet."
        ),
    )
    auto_stop = forms.BooleanField(
        label=_("Stop this copy once the domain points to the new machine"),
        required=False,
        initial=True,
        help_text=_(
            "The copy on the new machine must answer first. The host of this panel needs "
            "the timer of a current install.sh for it."
        ),
    )
    understood = forms.BooleanField(
        label=_("The panel accepts no changes until the move is confirmed or cancelled"),
    )

    def __init__(self, *args: Any, panel: PanelMachine, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._panel = panel

    def clean_target(self) -> Server:
        target: Server = self.cleaned_data["target"]
        if self._panel.includes(target.host):
            raise forms.ValidationError(
                _(
                    "This panel already runs on %(host)s. "
                    "Choose another machine: the move installs the panel there."
                ),
                params={"host": target.host},
            )
        return target
