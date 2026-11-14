"""The form of a new server group."""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.groups.models import ServerGroup
from apps.groups.services.membership import standalone_instances
from apps.protocols.models import ProtocolInstance


class InstanceChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    def label_from_instance(self, obj: ProtocolInstance) -> str:
        return f"{obj.server.name} · {obj.title}"


class GroupForm(forms.ModelForm):  # type: ignore[type-arg]
    leader = InstanceChoiceField(
        label=_("Leading server"),
        queryset=ProtocolInstance.objects.none(),
        help_text=_(
            "A standalone AmneziaWG 3.1 server. Its keys, settings and clients become the group's."
        ),
    )

    class Meta:
        model = ServerGroup
        fields = ("name", "domain", "relay")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        ids = [instance.pk for instance in standalone_instances()]
        leader: InstanceChoiceField = self.fields["leader"]  # type: ignore[assignment]
        leader.queryset = ProtocolInstance.objects.filter(pk__in=ids).select_related("server")
