"""Settings forms of the protocols other than WireGuard, on the common base of all of them.

Fields and checks follow the settings pages of the Amnezia app; a change that clients cannot
connect with the old profile needs `confirm`.
"""

from abc import abstractmethod
from ipaddress import IPv4Network
from typing import Any, ClassVar

from django import forms
from django.utils.translation import gettext_lazy as _

from amnezia_core.awg_params import MAX_PORT
from amnezia_core.openvpn import CIPHERS, HASHES, TRANSPORTS
from amnezia_core.xray import FINGERPRINTS, FLOWS, is_valid_site
from apps.accounts.models import User
from apps.operations.models import Operation
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.openvpn_settings import SUBNET_PREFIX, OpenVpnPanelSettings
from apps.protocols.services.settings_change import PanelSettings, start_settings_change
from apps.protocols.services.xray_settings import XrayPanelSettings

_PORT_RANGE = _("Port must be in the range of 1 to 65535")
_PORT = _("Port")
_EXTRA_CONFIG_LENGTH = 4000
_SHELL_CHARACTERS = "$`\\"
_HEREDOC_END = "EOF"


def _config_lines(text: str) -> str:
    """Lines of an extra OpenVPN config without carriage returns and outer blank lines."""
    return text.replace("\r", "").strip()


def port_field(label: Any = _PORT) -> forms.IntegerField:
    """A port field with the message of the app for a port out of range."""
    messages = {"min_value": _PORT_RANGE, "max_value": _PORT_RANGE}
    return forms.IntegerField(
        label=label,
        min_value=1,
        max_value=MAX_PORT,
        error_messages=messages,
        widget=forms.NumberInput(attrs={"x-model": "port"}),
    )


class SettingsForm(forms.Form):
    """Container log, reissue of imported keys and the confirmation, common to all protocols.

    A subclass declares its fields and `fields_template`, fills `initial_of`, says whether the
    change `changes_server` and starts the operation in `start`.
    """

    # The template with the fields of the protocol, included by protocols/settings.html.
    fields_template: ClassVar[str]

    container_logs = forms.BooleanField(
        label=_("Keep the container log"),
        required=False,
        help_text=_(
            "Amnezia runs the container without a log. With the log on, docker keeps up to 30 MB "
            "of it. Changing this recreates the container; keys and clients stay."
        ),
    )
    reissue = forms.BooleanField(
        label=_("Reissue the keys imported from the server"),
        required=False,
        help_text=_("They have no private key in the panel, so their profile cannot be updated."),
    )
    confirm = forms.BooleanField(
        label=_("I understand that every client must import the key again"), required=False
    )

    def __init__(self, *args: Any, instance: ProtocolInstance, **kwargs: Any) -> None:
        self.instance = instance
        initial = {"container_logs": instance.container_logs, **self.initial_of(instance)}
        kwargs["initial"] = {**initial, **(kwargs.get("initial") or {})}
        super().__init__(*args, **kwargs)

    def initial_of(self, instance: ProtocolInstance) -> dict[str, Any]:
        """The current values of the protocol fields."""
        return {"port": instance.port}

    def check(self, data: dict[str, Any]) -> None:
        """Checks of several fields together, before the confirmation is asked for."""

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        self.check(data)
        port = data.get("port")
        if port is not None and port == self.instance.server.probe_port:
            self.add_error("port", _("The reachability probe of the server uses this port."))
        if not self.errors and self.changes_server and not data.get("confirm"):
            self.add_error("confirm", _("Confirm that the keys must be imported again."))
        return data

    @property
    @abstractmethod
    def settings(self) -> PanelSettings:
        """The settings of the valid form."""

    @property
    @abstractmethod
    def changes_server(self) -> bool:
        """Clients cannot connect with the profile they have."""

    def start(self, user: User) -> Operation:
        """Start the operation that applies the settings."""
        reissue = self.cleaned_data["reissue"]
        return start_settings_change(self.instance, self.settings, user, reissue=reissue)


class XraySettingsForm(SettingsForm):
    """Port, masking site, SNI, flow and fingerprint of XRay Reality, as on the app's page."""

    fields_template = "protocols/_settings_xray.html"

    port = port_field()
    site = forms.CharField(label=_("Masking site"), max_length=253)
    sni = forms.CharField(
        label=_("SNI"),
        max_length=253,
        required=False,
        help_text=_("Empty: the masking site."),
    )
    flow = forms.ChoiceField(
        label=_("Flow"),
        required=False,
        choices=[(flow, flow or _("none")) for flow in FLOWS],
    )
    fingerprint = forms.ChoiceField(
        label=_("Fingerprint"),
        choices=[(name, name) for name in FINGERPRINTS],
        help_text=_("Only the client profiles get it: the server is not restarted for it."),
    )

    def initial_of(self, instance: ProtocolInstance) -> dict[str, Any]:
        return XrayPanelSettings.of(instance).to_params()

    def clean_site(self) -> str:
        site: str = self.cleaned_data["site"].strip()
        if not is_valid_site(site):
            raise forms.ValidationError(
                _("Enter a domain name like www.example.com or an IPv4 address.")
            )
        return site

    def clean_sni(self) -> str:
        sni: str = self.cleaned_data["sni"].strip()
        if not sni:
            return ""
        if "*" in sni:
            raise forms.ValidationError(_("SNI must not contain *."))
        if not is_valid_site(sni):
            raise forms.ValidationError(_("SNI must be a valid domain or an IP address."))
        return sni

    @property
    def settings(self) -> XrayPanelSettings:
        data = self.cleaned_data
        return XrayPanelSettings.build(
            port=data["port"],
            flow=data.get("flow") or "",
            site=data["site"],
            sni=data.get("sni") or "",
            fingerprint=data["fingerprint"],
            logs=data["container_logs"],
        )

    @property
    def changes_server(self) -> bool:
        return self.settings.changes_server(XrayPanelSettings.of(self.instance))


class OpenVpnSettingsForm(SettingsForm):
    """Transport, port, subnet, encryption and TLS auth of OpenVPN, as on the app's page."""

    fields_template = "protocols/_settings_openvpn.html"

    transport = forms.ChoiceField(
        label=_("Transport"),
        choices=[(name, name.upper()) for name in TRANSPORTS],
        widget=forms.RadioSelect,
    )
    port = port_field()
    subnet = forms.GenericIPAddressField(
        label=_("VPN address subnet"),
        protocol="IPv4",
        help_text=_("The mask is /24, as in the app."),
    )
    auto_negotiate = forms.BooleanField(
        label=_("Auto-negotiate encryption"),
        required=False,
        help_text=_("Client and server agree on the cipher; the one below is the fallback."),
    )
    hash = forms.ChoiceField(label=_("Hash"), choices=[(name, name) for name in HASHES])
    cipher = forms.ChoiceField(label=_("Cipher"), choices=[(name, name) for name in CIPHERS])
    tls_auth = forms.BooleanField(label=_("TLS auth"), required=False)
    block_outside_dns = forms.BooleanField(
        label=_("Block DNS requests outside of VPN"),
        required=False,
        help_text=_("Windows clients of the Amnezia app only."),
    )
    additional_client_config = forms.CharField(
        label=_("Additional client configuration commands"),
        required=False,
        max_length=_EXTRA_CONFIG_LENGTH,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    additional_server_config = forms.CharField(
        label=_("Additional server configuration commands"),
        required=False,
        max_length=_EXTRA_CONFIG_LENGTH,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Lines of server.conf, without the characters $ ` and \\."),
    )

    def initial_of(self, instance: ProtocolInstance) -> dict[str, Any]:
        params = OpenVpnPanelSettings.of(instance).to_params()
        return {**params, "auto_negotiate": not params["ncp_disable"]}

    def clean_additional_client_config(self) -> str:
        return _config_lines(self.cleaned_data["additional_client_config"])

    def clean_additional_server_config(self) -> str:
        """configure_container.sh puts the lines into an unquoted here-document."""
        text = _config_lines(self.cleaned_data["additional_server_config"])
        if any(char in text for char in _SHELL_CHARACTERS) or _HEREDOC_END in text.split("\n"):
            raise forms.ValidationError(_("The commands cannot contain $, ` or \\ or a line EOF."))
        return text

    def clean_subnet(self) -> str:
        """Any address of the subnet; the network address is kept."""
        address = self.cleaned_data["subnet"]
        return str(IPv4Network(f"{address}/{SUBNET_PREFIX}", strict=False).network_address)

    @property
    def settings(self) -> OpenVpnPanelSettings:
        data = self.cleaned_data
        return OpenVpnPanelSettings.from_params(
            {**data, "ncp_disable": not data["auto_negotiate"], "logs": data["container_logs"]}
        )

    @property
    def changes_server(self) -> bool:
        return self.settings.changes_profiles(OpenVpnPanelSettings.of(self.instance))
