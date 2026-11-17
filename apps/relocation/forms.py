"""Form of a panel move: the new machine, chosen from the servers of the panel."""

from django import forms
from django.utils.translation import gettext_lazy as _

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
    understood = forms.BooleanField(
        label=_("The panel accepts no changes until the move is confirmed or cancelled"),
    )
