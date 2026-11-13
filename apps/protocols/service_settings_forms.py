"""Settings forms of the proxies, with the fields and checks of their pages in the Amnezia app.

SOCKS5 has its port and account, MTProxy and Telemt the settings of the install form and TProxy
its domain. A port taken by another container of the server is refused, as at the install.
"""

import copy
import re
from abc import abstractmethod
from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from amnezia_core.openvpn import is_ipv4
from amnezia_core.proxy_options import TelegramProxyOptions, parse_secret_list
from amnezia_core.xray import is_valid_site
from apps.accounts.models import User
from apps.operations.models import Operation
from apps.protocols.install_form import InstallForm, proxy_errors
from apps.protocols.models import ServiceInstance
from apps.protocols.services.installation import used_ports
from apps.protocols.services.installer import installers
from apps.protocols.services.service_settings import (
    ServiceSettings,
    start_service_settings_change,
)
from apps.protocols.settings_forms import port_field

_TEXT = forms.TextInput(attrs={"spellcheck": "false", "autocomplete": "off"})
# 3proxy.cfg keeps the account as `users user:CL:password`; the app and the panel read it back
# with `users (\w+):CL:(\w+)`, so any other character would cut the password.
_ACCOUNT_PART = re.compile(r"\w+", re.ASCII)


class ServiceSettingsForm(forms.Form):
    """The port, common to the proxies; a subclass adds its fields."""

    port = port_field()

    def __init__(self, *args: Any, service: ServiceInstance, **kwargs: Any) -> None:
        self.service = service
        self.installer = installers.get(service.container)
        initial = {"port": service.port, **self.initial_of(service)}
        kwargs["initial"] = {**initial, **(kwargs.get("initial") or {})}
        super().__init__(*args, **kwargs)

    def initial_of(self, service: ServiceInstance) -> dict[str, Any]:
        """The current values of the fields besides the port."""
        return {}

    @property
    def extra_fields(self) -> list[forms.BoundField]:
        """The fields besides the port, shown under it."""
        return [self[name] for name in self.fields if name != "port"]

    def clean_port(self) -> int:
        port: int = self.cleaned_data["port"]
        own = self.installer.published_ports(self.service.port or 0)
        taken = used_ports(self.service.server) - own
        if self.installer.published_ports(port) & taken:
            raise forms.ValidationError(_("This port is already used on the server."))
        return port

    @property
    @abstractmethod
    def settings(self) -> ServiceSettings:
        """The settings of the valid form."""

    def start(self, user: User, *, ip: str | None = None) -> Operation:
        return start_service_settings_change(self.service, self.settings, user, ip=ip)


class Socks5SettingsForm(ServiceSettingsForm):
    """Port, user name and password of SOCKS5, as on the app's page."""

    user = forms.CharField(label=_("User name"), max_length=64, widget=_TEXT)
    password = forms.CharField(label=_("Password"), max_length=64, widget=_TEXT)

    def initial_of(self, service: ServiceInstance) -> dict[str, Any]:
        return {"user": service.login, "password": service.secret}

    def clean_user(self) -> str:
        return self._account_part("user")

    def clean_password(self) -> str:
        return self._account_part("password")

    def _account_part(self, name: str) -> str:
        value: str = self.cleaned_data[name]
        if not _ACCOUNT_PART.fullmatch(value):
            raise forms.ValidationError(_("Only Latin letters, digits and underscores."))
        return value

    @property
    def settings(self) -> ServiceSettings:
        data = self.cleaned_data
        return ServiceSettings(data["port"], {"user": data["user"]}, data["password"])


class MtProtoSettingsForm(ServiceSettingsForm):
    """MTProxy and Telemt: the settings of the install form, those the proxy has, as in the app.

    A setting the import of an older panel did not keep is not overwritten with an empty field.
    """

    def __init__(self, *args: Any, service: ServiceInstance, **kwargs: Any) -> None:
        super().__init__(*args, service=service, **kwargs)
        for name in self.installer.install_fields:
            field = copy.deepcopy(InstallForm.base_fields[name])
            if name in ("mode", "tls_domain"):
                field.required = True
            self.fields[name] = field

    def initial_of(self, service: ServiceInstance) -> dict[str, Any]:
        return dict(service.settings)

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if not self.errors:
            for name, error in proxy_errors(self._options(), self.installer.install_fields):
                self.add_error(name, error)
        return data

    def _options(self) -> TelegramProxyOptions:
        values = {name: self._text(name) for name in self.installer.install_fields}
        values = {
            name: value.lower() if "ip" not in name else value for name, value in values.items()
        }
        secrets = parse_secret_list(values.pop("additional_secrets", ""))
        return TelegramProxyOptions.from_params({**values, "additional_secrets": secrets})

    def _text(self, name: str) -> str:
        return str(self.cleaned_data.get(name) or "").strip()

    @property
    def settings(self) -> ServiceSettings:
        params = self._options().to_params()
        known = self.service.settings
        options = {
            name: params[name]
            for name in self.installer.install_fields
            if name in known or self._text(name)
        }
        return ServiceSettings(self.cleaned_data["port"], options)


class TProxySettingsForm(ServiceSettingsForm):
    """Port and domain of TProxy; Caddy gets a certificate for a new domain."""

    domain = forms.CharField(
        label=_("Proxy domain"),
        max_length=253,
        widget=_TEXT,
        help_text=_("A domain that points at this server."),
    )
    email = forms.EmailField(
        label=_("E-mail for the certificate"),
        required=False,
        help_text=_("Empty: the e-mail given at the install."),
    )

    def initial_of(self, service: ServiceInstance) -> dict[str, Any]:
        return {"domain": service.settings.get("hostname", "")}

    def clean_domain(self) -> str:
        domain = str(self.cleaned_data["domain"]).strip().lower()
        if not is_valid_site(domain) or is_ipv4(domain):
            raise forms.ValidationError(_("Enter a domain name like proxy.example.com."))
        return domain

    @property
    def settings(self) -> ServiceSettings:
        data = self.cleaned_data
        options = {"hostname": data["domain"], "email": data.get("email") or ""}
        return ServiceSettings(data["port"], options)
