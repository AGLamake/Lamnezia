"""Forms of the server groups: a new group, its name, domain and relay, its protocol settings."""

from ipaddress import IPv4Network
from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import User
from apps.groups.models import ServerGroup
from apps.groups.services.membership import standalone_instances
from apps.groups.services.settings import leading_instance, start_group_settings
from apps.operations.models import Operation
from apps.protocols.forms import ProtocolSettingsForm
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.awg_settings import AwgSettings


class InstanceChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    def label_from_instance(self, obj: ProtocolInstance) -> str:
        return f"{obj.server.name} · {obj.title}"


class GroupEditForm(forms.ModelForm):  # type: ignore[type-arg]
    """The name, domain and relay of a group."""

    class Meta:
        model = ServerGroup
        fields = ("name", "domain", "relay")


class GroupForm(GroupEditForm):
    leader = InstanceChoiceField(
        label=_("Leading server"),
        queryset=ProtocolInstance.objects.none(),
        help_text=_(
            "A standalone AmneziaWG 3.1 server. Its keys, settings and clients become the group's."
        ),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        ids = [instance.pk for instance in standalone_instances()]
        leader: InstanceChoiceField = self.fields["leader"]  # type: ignore[assignment]
        leader.queryset = ProtocolInstance.objects.filter(pk__in=ids).select_related("server")


class GroupSettingsForm(ProtocolSettingsForm):
    """DNS, subnet and AmneziaWG parameters of every member of a group, on one form.

    The port is the port of the group and the container log belongs to each server: neither is
    on the form. The subnet must stay apart from the other networks of every member server.
    """

    def __init__(self, *args: Any, group: ServerGroup, **kwargs: Any) -> None:
        self.group = group
        super().__init__(*args, instance=leading_instance(group), **kwargs)
        del self.fields["port"]
        del self.fields["container_logs"]

    def _other_subnets(self) -> list[IPv4Network]:
        members = ProtocolInstance.objects.filter(group_member__group=self.group)
        return [subnet for member in members for subnet in self.other_subnets(member)]

    @property
    def settings(self) -> AwgSettings:
        data = self.cleaned_data
        params = {name: data.get(name) or "" for name in self.param_names}
        dns = (data["primary_dns"], data["secondary_dns"])
        return AwgSettings.build(
            self.instance.port,
            params,
            dns,
            logs=self.instance.container_logs,
            subnet=data["subnet"],
        )

    def start(self, user: User, ip: str | None = None) -> Operation:
        reissue = self.cleaned_data["reissue"]
        return start_group_settings(self.group, self.settings, user=user, reissue=reissue, ip=ip)
