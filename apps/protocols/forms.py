from dataclasses import dataclass
from ipaddress import IPv4Network
from typing import Any

from django import forms
from django.forms.boundfield import BoundField
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from amnezia_core.awg import SPECIAL_JUNK, interface_params
from amnezia_core.awg_export import AWG3_PARAMS
from amnezia_core.awg_params import MAX_PORT, Problem, check_awg_params
from amnezia_core.containers import WG_CONTAINERS
from amnezia_core.script_vars import TOGGLES
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.awg_settings import AwgSettings
from apps.protocols.services.host_scripts import AMNEZIA_NETWORK_SUBNET
from apps.protocols.services.installation import used_ports
from apps.protocols.services.speed_test import (
    DEFAULT_MINUTES,
    MAX_MINUTES,
    MAX_PORTS,
    SpeedTestPlan,
    default_ports,
)
from apps.protocols.services.speed_test import TRANSPORTS as SPEED_TEST_TRANSPORTS
from apps.protocols.settings_forms import SettingsForm, port_field
from apps.servers.models import Server

PROBLEMS: dict[Problem, Promise] = {
    Problem.NOT_A_NUMBER: _("Enter a whole number."),
    Problem.TOO_LARGE: _("The value is too large: the packet must fit into 1280 bytes."),
    Problem.NOT_A_RANGE: _("Enter a number or a range like 100-200."),
    Problem.MIN_ABOVE_MAX: _("Jmin must not be larger than Jmax."),
    Problem.HEADERS_OVERLAP: _("H1-H4 must be different and their ranges must not overlap."),
    Problem.SIZES_COLLIDE: _(
        "With these S1-S4 two kinds of packets get the same size: change one of them."
    ),
    Problem.NOT_A_KEY: _("Enter a 32-byte key in base64."),
    Problem.NOT_A_TOGGLE: _("Choose on or off."),
    Problem.NOT_JUNK_TAGS: _("Use only the tags <b 0x..>, <r N>, <rc N>, <rd N>, <c> and <t>."),
    Problem.PADDING_TOO_SMALL: _(
        "With HeaderProtectionKey each of S1-S4 must be at least 12, or AmneziaWG does not start."
    ),
}

# How the AWG parameters are grouped on the page.
_GROUPS: tuple[tuple[Promise, tuple[str, ...]], ...] = (
    (_("Junk packets"), ("Jc", "Jmin", "Jmax")),
    (_("Message padding"), ("S1", "S2", "S3", "S4")),
    (_("Message headers"), ("H1", "H2", "H3", "H4")),
    (_("Special junk packets"), SPECIAL_JUNK),
    (_("AmneziaWG 3.1"), AWG3_PARAMS),
)
# A subnet larger than this is not a VPN of one server.
_LARGEST_SUBNET = 16
_AWG3_NOTE = _(
    "The server runs AmneziaWG 2.0: move it to 3.1 with the button on the server page first. "
    "Its image may not know these parameters, and AmneziaWG then does not start."
)


@dataclass(frozen=True)
class ParamGroup:
    """A fieldset of the AWG parameters; `note` explains what the group needs, if anything."""

    title: Promise
    fields: list[BoundField]
    note: Promise | None = None


# With a context: the bare "on" and "off" of the site catalog are worded for other nouns.
_TOGGLE_CHOICES = (
    ("", _("not set")),
    ("on", pgettext_lazy("toggle", "on")),
    ("off", pgettext_lazy("toggle", "off")),
)


class ProtocolSettingsForm(SettingsForm):
    """Port, DNS and AWG parameters of a WireGuard-family instance.

    A change of the port or the parameters needs `confirm`: every client must import the key
    again, and keys imported from the server stop working unless they are reissued.
    """

    fields_template = "protocols/_settings_wg.html"

    port = port_field(_("UDP port"))
    subnet = forms.CharField(
        label=_("Client subnet"),
        max_length=18,
        help_text=_(
            "Every client keeps its place in the new subnet: 10.8.1.5 of 10.8.1.0/24 becomes "
            "10.9.0.5 of 10.9.0.0/24. Changing it recreates the container."
        ),
    )
    primary_dns = forms.GenericIPAddressField(label=_("DNS"), protocol="IPv4")
    secondary_dns = forms.GenericIPAddressField(label=_("Secondary DNS"), protocol="IPv4")

    def __init__(self, *args: Any, instance: ProtocolInstance, **kwargs: Any) -> None:
        super().__init__(*args, instance=instance, **kwargs)
        self.param_names = interface_params(WG_CONTAINERS[instance.container])
        for name in self.param_names:
            self.fields[name] = _param_field(name)

    def initial_of(self, instance: ProtocolInstance) -> dict[str, Any]:
        return {
            "port": instance.port,
            "subnet": instance.subnet,
            "primary_dns": instance.primary_dns,
            "secondary_dns": instance.secondary_dns,
            **instance.params,
        }

    @property
    def groups(self) -> list[ParamGroup]:
        grouped = {name for _title, names in _GROUPS for name in names}
        other = tuple(name for name in self.param_names if name not in grouped)
        groups = (*_GROUPS, (_("Other parameters"), other))
        return [
            ParamGroup(
                title,
                [self[name] for name in names if name in self.fields],
                _AWG3_NOTE if names == AWG3_PARAMS and self.instance.awg_outdated else None,
            )
            for title, names in groups
            if any(name in self.fields for name in names)
        ]

    def clean_subnet(self) -> str:
        """A private network no smaller than the current one, apart from the other subnets."""
        try:
            subnet = IPv4Network(self.cleaned_data["subnet"].strip())
        except ValueError:
            raise forms.ValidationError(_("Enter a network like 10.8.1.0/24.")) from None
        current = IPv4Network(self.instance.subnet)
        if subnet == current:
            return str(subnet)
        if not subnet.is_private or subnet.prefixlen < _LARGEST_SUBNET:
            raise forms.ValidationError(
                _("Use a private network from /%(largest)d to /%(smallest)d.")
                % {"largest": _LARGEST_SUBNET, "smallest": current.prefixlen}
            )
        if subnet.num_addresses < current.num_addresses:
            raise forms.ValidationError(
                _("The subnet must be at least /%(prefix)d: the clients must fit into it.")
                % {"prefix": current.prefixlen}
            )
        if any(subnet.overlaps(other) for other in self._other_subnets()):
            raise forms.ValidationError(_("The subnet overlaps another network of the server."))
        return str(subnet)

    def _other_subnets(self) -> list[IPv4Network]:
        instances = ProtocolInstance.objects.filter(server=self.instance.server).exclude(
            pk=self.instance.pk
        )
        subnets = [IPv4Network(AMNEZIA_NETWORK_SUBNET)]
        for value in instances.values_list("subnet", flat=True):
            try:
                subnets.append(IPv4Network(value))
            except ValueError:
                continue
        return subnets

    def check(self, data: dict[str, Any]) -> None:
        params = {name: data.get(name) or "" for name in self.param_names}
        for problem in check_awg_params(params):
            if problem.param not in self.errors:
                self.add_error(problem.param, str(PROBLEMS[problem.problem]))

    @property
    def settings(self) -> AwgSettings:
        data = self.cleaned_data
        params = {name: data.get(name) or "" for name in self.param_names}
        dns = (data["primary_dns"], data["secondary_dns"])
        return AwgSettings.build(
            data["port"], params, dns, logs=data["container_logs"], subnet=data["subnet"]
        )

    @property
    def changes_server(self) -> bool:
        return self.settings.changes_server(AwgSettings.of(self.instance))


class RemoveContainerForm(forms.Form):
    """Removal destroys the clients of the container: the administrator types the server name."""

    server_name = forms.CharField(label=_("Server name"))
    delete_backups = forms.BooleanField(
        label=_("Delete all backups?"),
        required=False,
        help_text=_(
            "The keys are revoked and the panel forgets the protocol. Unchecked, the keys and "
            "the backup stay, and the protocol can be restored on this server."
        ),
    )

    def __init__(self, *args: Any, instance: ProtocolInstance, **kwargs: Any) -> None:
        self.instance = instance
        super().__init__(*args, **kwargs)

    def clean_server_name(self) -> str:
        name: str = self.cleaned_data["server_name"].strip()
        if name != self.instance.server.name:
            raise forms.ValidationError(_("Type the name of the server exactly as it is shown."))
        return name


def _reissue_imported() -> forms.BooleanField:
    return forms.BooleanField(
        label=_("Reissue the keys imported from the server"),
        required=False,
        help_text=_("They have no private key in the panel, so their profile cannot be updated."),
    )


class ReaddressForm(forms.Form):
    """After the server address changes: imported keys reissued, the IPsec certificate too."""

    reissue = _reissue_imported()
    ipsec = forms.BooleanField(
        label=_("Reissue the IPsec server certificate for the new address"),
        required=False,
        initial=True,
        help_text=_("Needed if the server IP changed; IPsec clients are refused until then."),
    )


class IpsecReissueForm(forms.Form):
    """The IPsec server certificate for the server IP: whether imported keys are reissued."""

    reissue = _reissue_imported()


class AwgUpgradeForm(forms.Form):
    """The move of AmneziaWG 2.0 to 3.1: whether imported keys are reissued."""

    reissue = _reissue_imported()


class AwgMigrationForm(forms.Form):
    """The move of legacy AmneziaWG clients: the port of amnezia-awg2 if it is to be installed."""

    port = forms.IntegerField(label=_("UDP port"), min_value=1, max_value=MAX_PORT)

    def __init__(self, *args: Any, server: Server, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.server = server

    def clean_port(self) -> int:
        port: int = self.cleaned_data["port"]
        if (port, "udp") in used_ports(self.server):
            raise forms.ValidationError(_("The port is already used on the server."))
        return port


class SpeedTestForm(forms.Form):
    """The ports of the iperf3 speed test, each free on the server for TCP and UDP."""

    ports = forms.CharField(
        label=_("Ports"),
        max_length=100,
        widget=forms.TextInput(attrs={"spellcheck": "false", "autocomplete": "off"}),
        help_text=_("Up to four ports separated by commas: one up to 9999 and one above it."),
    )
    minutes = forms.IntegerField(
        label=_("Minutes"),
        min_value=1,
        max_value=MAX_MINUTES,
        initial=DEFAULT_MINUTES,
        help_text=_("Then the container removes itself."),
    )

    def __init__(self, *args: Any, server: Server, **kwargs: Any) -> None:
        self.server = server
        super().__init__(*args, **kwargs)
        self.fields["ports"].initial = ", ".join(map(str, default_ports(server)))

    def clean_ports(self) -> tuple[int, ...]:
        parts = [part for part in self.cleaned_data["ports"].replace(" ", "").split(",") if part]
        if not all(part.isdigit() and 1 <= int(part) <= MAX_PORT for part in parts):
            raise forms.ValidationError(_("Enter port numbers from 1 to 65535."))
        ports = tuple(dict.fromkeys(int(part) for part in parts))
        if not 1 <= len(ports) <= MAX_PORTS:
            raise forms.ValidationError(_("Enter from one to four ports."))
        taken = used_ports(self.server) | {(self.server.port, "tcp")}
        busy = [port for port in ports if any((port, t) in taken for t in SPEED_TEST_TRANSPORTS)]
        if busy:
            raise forms.ValidationError(
                _("Already used on the server: %(ports)s.") % {"ports": ", ".join(map(str, busy))}
            )
        return ports

    @property
    def plan(self) -> SpeedTestPlan:
        return SpeedTestPlan(self.cleaned_data["ports"], self.cleaned_data["minutes"])


def _param_field(name: str) -> forms.Field:
    if name in TOGGLES:
        return forms.ChoiceField(label=name, choices=_TOGGLE_CHOICES, required=False)
    widget = forms.TextInput(attrs={"spellcheck": "false", "autocomplete": "off"})
    return forms.CharField(label=name, required=False, widget=widget)
