"""The install form of the server card: what to install, on which port, and the installer's fields.

Each installer class names the fields it reads (`install_fields`): the page shows a field only
for those installers, and the form checks only the fields of the chosen one.
"""

from collections.abc import Collection
from typing import Any

from django import forms
from django.forms.boundfield import BoundField
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from amnezia_core.awg_params import MAX_PORT
from amnezia_core.openvpn import is_ipv4
from amnezia_core.proxies import DEFAULT_TLS_DOMAIN, FAKETLS, STANDARD
from amnezia_core.proxy_options import TelegramProxyOptions, is_hex32, parse_secret_list
from amnezia_core.xray import DEFAULT_SITE, is_valid_site
from apps.protocols.catalog import ContainerGroup
from apps.protocols.services.installation import InstallChoice, InstallRequest, used_ports
from apps.protocols.services.installer import InstallerClass, installers
from apps.servers.models import Server

_TEXT = forms.TextInput(attrs={"spellcheck": "false", "autocomplete": "off"})
# The fields every installer has; the others are shown for the installers that name them.
_COMMON_FIELDS = ("container", "port", "logs")


class InstallForm(forms.Form):
    """What to install and on which port; an empty port means the port Amnezia uses."""

    container = forms.ChoiceField(
        label=_("Protocol or service"), widget=forms.RadioSelect(attrs={"x-model": "container"})
    )
    port = forms.IntegerField(
        label=_("Port"),
        min_value=1,
        max_value=MAX_PORT,
        required=False,
        widget=forms.NumberInput(attrs={"x-model": "port"}),
        help_text=_(
            "Leave empty for the port in the table below: a free port up to 9999 for "
            "AmneziaWG, the port of the Amnezia app for the others."
        ),
    )
    logs = forms.BooleanField(
        label=_("Keep the container log"),
        required=False,
        help_text=_("Amnezia runs the container without a log; docker keeps up to 30 MB of it."),
    )
    site = forms.CharField(
        label=_("Masking site"),
        required=False,
        initial=DEFAULT_SITE,
        max_length=253,
        widget=_TEXT,
        help_text=_(
            "XRay only: the site the server pretends to be for an observer. Choose a large "
            "site that works over HTTPS; leave empty for the one the Amnezia app uses."
        ),
    )
    domain = forms.CharField(
        label=_("Proxy domain"),
        required=False,
        max_length=253,
        widget=_TEXT,
        help_text=_(
            "TProxy only: a domain that points at this server. Port 80 must be free for its "
            "Let's Encrypt certificate."
        ),
    )
    email = forms.EmailField(
        label=_("E-mail for the certificate"),
        required=False,
        help_text=_("TProxy only: Let's Encrypt writes here about the certificate."),
    )
    mode = forms.ChoiceField(
        label=_("Transport mode"),
        required=False,
        initial=STANDARD,
        choices=((STANDARD, _("Standard (dd secret)")), (FAKETLS, _("FakeTLS (ee secret)"))),
        help_text=_("FakeTLS makes the traffic look like HTTPS to the domain below."),
    )
    tls_domain = forms.CharField(
        label=_("FakeTLS domain"),
        required=False,
        initial=DEFAULT_TLS_DOMAIN,
        max_length=253,
        widget=_TEXT,
        help_text=_("A large site that works over HTTPS; it goes into the secret of the links."),
    )
    public_host = forms.CharField(
        label=_("Public host"),
        required=False,
        max_length=253,
        widget=_TEXT,
        help_text=_("The domain or IP address in the links; empty for the address of the server."),
    )
    tag = forms.CharField(
        label=_("Promotion tag"),
        required=False,
        max_length=32,
        widget=_TEXT,
        help_text=_("32 hex digits from @MTProxybot: Telegram shows your channel to the users."),
    )
    additional_secrets = forms.CharField(
        label=_("Additional secrets"),
        required=False,
        max_length=1000,
        widget=_TEXT,
        help_text=_("Secrets of 32 hex digits separated by commas; the links use the main one."),
    )
    nat_internal_ip = forms.GenericIPAddressField(
        label=_("NAT: internal address"),
        protocol="IPv4",
        required=False,
        help_text=_("For a server behind NAT: its address in the local network."),
    )
    nat_external_ip = forms.GenericIPAddressField(
        label=_("NAT: external address"),
        protocol="IPv4",
        required=False,
        help_text=_("For a server behind NAT: the public address Telegram sees."),
    )
    offload = forms.BooleanField(
        label=_("Turn TSO/GSO off in the container (recommended)"),
        required=False,
        initial=True,
        help_text=_(
            "AmneziaWG and WireGuard only. On some VPS upload through the VPN drops several "
            "times because the container network glues TCP packets together and the glued "
            "packets are lost. A systemd timer on the server keeps TSO/GSO off on the container "
            "interface. Configs and keys do not change; it is turned off on the server card."
        ),
    )

    def __init__(
        self, *args: Any, server: Server, choices: list[InstallChoice], **kwargs: Any
    ) -> None:
        self.server = server
        super().__init__(*args, **kwargs)
        offered = [choice for choice in choices if choice.available]
        self._offered = [choice.installer for choice in offered]
        field = self.fields["container"]
        assert isinstance(field, forms.ChoiceField)
        field.choices = [
            (group.label, [(c.container, c.title) for c in offered if c.group == group])
            for group in ContainerGroup
            if any(c.group == group for c in offered)
        ]
        self.descriptions = [(c.container, c.description) for c in offered]

    @cached_property
    def taken(self) -> set[tuple[int, str]]:
        return used_ports(self.server)

    @property
    def low_port_advice(self) -> list[tuple[str, int, int]]:
        """(container, app port, free low port) of protocols that keep a high app port."""
        advice = []
        for installer in self._offered:
            port = installer.low_port_advice(self.taken)
            if port is not None:
                advice.append((installer.container, installer.default_port, port))
        return advice

    @property
    def installer_fields(self) -> list[tuple[BoundField, str]]:
        """The fields of some installers, each with the Alpine condition that shows it."""
        shown = []
        for name in self.fields:
            if name in _COMMON_FIELDS:
                continue
            containers = [i.container for i in installers if name in i.install_fields]
            condition = f"{containers!r}.includes(container)"
            shown.append((self[name], condition))
        return shown

    @property
    def request(self) -> InstallRequest:
        data = self.cleaned_data
        installer = installers.get(data["container"])
        choosable = installer.publishes_port and not installer.fixed_port
        port = (choosable and data["port"]) or installer.suggested_port(self.taken)
        asks = installer.install_fields
        return InstallRequest(
            installer,
            port,
            logs=data["logs"],
            site=(self._text("site") or DEFAULT_SITE) if "site" in asks else "",
            domain=self._text("domain").lower() if "domain" in asks else "",
            email=self._text("email") if "email" in asks else "",
            offload="offload" in asks and bool(data.get("offload")),
            proxy=self._proxy_options(installer) if "mode" in asks else None,
        )

    def _text(self, name: str) -> str:
        return str(self.cleaned_data.get(name) or "").strip()

    def _proxy_options(self, installer: InstallerClass) -> TelegramProxyOptions:
        def value(name: str) -> str:
            return self._text(name) if name in installer.install_fields else ""

        return TelegramProxyOptions(
            mode=value("mode") or STANDARD,
            tls_domain=value("tls_domain").lower() or DEFAULT_TLS_DOMAIN,
            public_host=value("public_host").lower(),
            tag=value("tag").lower(),
            additional_secrets=parse_secret_list(value("additional_secrets").lower()),
            nat_internal_ip=value("nat_internal_ip"),
            nat_external_ip=value("nat_external_ip"),
        )

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if "container" not in data or "port" in self.errors:
            return data
        request = self.request
        if request.installer.published_ports(request.port) & self.taken:
            self.add_error("port", _("This port is already used on the server."))
        if request.site and not is_valid_site(request.site):
            self.add_error(
                "site", _("Enter a domain name like www.example.com or an IPv4 address.")
            )
        if "domain" in request.installer.install_fields:
            self._check_domain(request)
        if request.proxy:
            self._check_proxy(request.proxy, request.installer)
        return data

    def _check_domain(self, request: InstallRequest) -> None:
        """TProxy needs a domain, not an address, and an e-mail for its certificate."""
        if not is_valid_site(request.domain) or is_ipv4(request.domain):
            self.add_error("domain", _("Enter a domain name like proxy.example.com."))
        if not request.email and "email" not in self.errors:
            self.add_error("email", _("Enter the e-mail for the certificate."))

    def _check_proxy(self, options: TelegramProxyOptions, installer: InstallerClass) -> None:
        for name, error in proxy_errors(options, installer.install_fields):
            self.add_error(name, error)


def proxy_errors(options: TelegramProxyOptions, fields: Collection[str]) -> list[tuple[str, Any]]:
    """(field, message) of what MTProxy and Telemt would reject or the links would break on."""
    errors: list[tuple[str, Any]] = []
    if not is_valid_site(options.tls_domain) or is_ipv4(options.tls_domain):
        errors.append(("tls_domain", _("Enter a domain name like www.example.com.")))
    if options.public_host and not is_valid_site(options.public_host):
        errors.append(
            ("public_host", _("Enter a domain name like proxy.example.com or an IPv4 address."))
        )
    if options.tag and not is_hex32(options.tag):
        errors.append(("tag", _("Enter the 32 hex digits of the tag.")))
    if not all(map(is_hex32, options.additional_secrets)):
        errors.append(("additional_secrets", _("Each secret must be 32 hex digits.")))
    nat = ("nat_internal_ip", "nat_external_ip")
    if all(name in fields for name in nat):
        given = [name for name in nat if getattr(options, name)]
        if len(given) == 1:
            missing = nat[0] if given[0] == nat[1] else nat[1]
            errors.append((missing, _("NAT needs both the internal and the external address.")))
    return errors
