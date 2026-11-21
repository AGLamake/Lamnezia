"""Forms of the server groups: a new group, its name, domain and relay, its protocol settings."""

from dataclasses import replace
from ipaddress import IPv4Network
from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from amnezia_core.awg_install import DEFAULT_TIMERS, SHORT_TIMERS, has_short_timers
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
    """DNS, MTU, subnet and AmneziaWG parameters of every member of a group, on one form.

    The port is the port of the group and the container log belongs to each server: neither is
    on the form. The subnet must stay apart from the other networks of every member server.
    Turning `short_timers` on or off sets KeepaliveTimeout and RekeyTimeout; left as it is, the
    two fields keep what the administrator typed.
    """

    short_timers = forms.BooleanField(
        label=_("Short timers"),
        required=False,
        help_text=_(
            "After a switch of the relay, clients reconnect in 7-9 seconds instead of 18-22: "
            "KeepaliveTimeout 3-5 and RekeyTimeout 2-3. On mobile networks with losses clients "
            "make more handshakes. Turned off, the timers of a new server come back."
        ),
    )

    def __init__(self, *args: Any, group: ServerGroup, **kwargs: Any) -> None:
        self.group = group
        super().__init__(*args, instance=leading_instance(group), **kwargs)
        del self.fields["port"]
        del self.fields["container_logs"]

    def initial_of(self, instance: ProtocolInstance) -> dict[str, Any]:
        return {**super().initial_of(instance), "short_timers": has_short_timers(instance.params)}

    def check(self, data: dict[str, Any]) -> None:
        wanted = data.get("short_timers", False)
        if wanted != self.initial["short_timers"]:
            data.update(SHORT_TIMERS if wanted else DEFAULT_TIMERS)
        super().check(data)

    def _other_subnets(self) -> list[IPv4Network]:
        members = ProtocolInstance.objects.filter(group_member__group=self.group)
        return [subnet for member in members for subnet in self.other_subnets(member)]

    @property
    def settings(self) -> AwgSettings:
        data = self.cleaned_data
        params = {name: data.get(name) or "" for name in self.param_names}
        dns = (data["primary_dns"], data["secondary_dns"])
        built = AwgSettings.build(
            self.instance.port,
            params,
            dns,
            logs=self.instance.container_logs,
            subnet=data["subnet"],
        )
        return replace(built, mtu=data["mtu"])

    def start(self, user: User, ip: str | None = None) -> Operation:
        reissue = self.cleaned_data["reissue"]
        return start_group_settings(self.group, self.settings, user=user, reissue=reissue, ip=ip)
