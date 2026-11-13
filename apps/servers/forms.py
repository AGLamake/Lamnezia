import re
from typing import Any, ClassVar

from django import forms
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.servers.models import AuthMethod, Server, ServerStatus
from apps.servers.services.probe import DEFAULT_PORT

_HOST = re.compile(r"^[A-Za-z0-9.:-]+$")
# Changing any of these needs a new check of the server.
CONNECTION_FIELDS = frozenset(
    {"host", "port", "username", "auth_method", "private_key", "passphrase", "password"}
)


class ServerForm(forms.ModelForm):  # type: ignore[type-arg]
    """Add or edit a server. Saved credentials are never sent back to the browser.

    Secret fields are not model fields of the form, so they never get the stored value as
    initial data. Left empty when editing, they keep what is stored.
    """

    private_key = forms.CharField(
        label=_("Private key"),
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 6, "autocomplete": "off", "spellcheck": "false"}),
        help_text=_("OpenSSH or PEM, Ed25519, ECDSA or RSA."),
    )
    passphrase = forms.CharField(
        label=_("Key passphrase"),
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
        help_text=_("Only if the key is protected by one."),
    )
    password = forms.CharField(
        label=_("Password"),
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    class Meta:
        model = Server
        fields = (
            "name",
            "location",
            "host",
            "port",
            "username",
            "auth_method",
            "paid_until",
            "maintenance",
        )
        widgets: ClassVar = {
            "auth_method": forms.Select(attrs={"x-model": "method"}),
            "paid_until": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            kept = _("Leave empty to keep the saved one.")
            for name in ("private_key", "password"):
                self.fields[name].help_text = kept

    def clean_host(self) -> str:
        host: str = self.cleaned_data["host"].strip().strip("[]")
        if not _HOST.match(host):
            raise forms.ValidationError(
                _("Enter a host name or an IP address, without a user name, port or protocol.")
            )
        return host

    def clean_private_key(self) -> str:
        key: str = self.cleaned_data["private_key"].replace("\r\n", "\n").strip()
        return f"{key}\n" if key else ""

    def clean(self) -> dict[str, Any]:
        super().clean()
        data = self.cleaned_data
        method = data.get("auth_method")
        if method == AuthMethod.KEY and not (data.get("private_key") or self._stored(method)):
            self.add_error("private_key", _("Paste the private key."))
        if method == AuthMethod.PASSWORD and not (data.get("password") or self._stored(method)):
            self.add_error("password", _("Enter the password."))
        return data

    def save(self, commit: bool = True) -> Server:
        server: Server = super().save(commit=False)
        data = self.cleaned_data
        if server.auth_method == AuthMethod.KEY:
            server.password = ""
            if data["private_key"]:
                server.private_key, server.passphrase = data["private_key"], data["passphrase"]
            elif data["passphrase"]:
                server.passphrase = data["passphrase"]
        else:
            server.private_key = server.passphrase = ""
            if data["password"]:
                server.password = data["password"]
        if commit:
            server.save()
        return server

    def _stored(self, method: str) -> bool:
        """Whether the server already has a secret for this login method."""
        stored = self.instance if self.instance.pk else None
        if stored is None or stored.auth_method != method:
            return False
        return bool(stored.private_key if method == AuthMethod.KEY else stored.password)


class ProbeForm(forms.Form):
    """UDP port of the reachability probe; it must not clash with SSH or an Amnezia container."""

    port = forms.IntegerField(
        label=_("UDP port"),
        min_value=1024,
        max_value=65535,
        initial=DEFAULT_PORT,
        help_text=_("Open it in the firewall of the server and of the hosting provider."),
    )

    def __init__(self, *args: Any, server: Server, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._server = server
        if server.probe_port:
            self.fields["port"].initial = server.probe_port

    def clean_port(self) -> int:
        port: int = self.cleaned_data["port"]
        taken = {str(self._server.port)} | {str(c.get("port")) for c in self._server.containers}
        if str(port) in taken:
            raise forms.ValidationError(_("This port is used by SSH or a protocol on the server."))
        return port


class ServerFilterForm(forms.Form):
    """Search of the server list by name, address or location, and its status; kept in the URL."""

    q = forms.CharField(label=_("Search"), required=False)
    status = forms.ChoiceField(
        label=_("Status"), required=False, choices=[("", _("Any")), *ServerStatus.choices]
    )

    def filter(self, servers: QuerySet[Server]) -> QuerySet[Server]:
        if not self.is_valid():
            return servers
        data = self.cleaned_data
        if text := data["q"]:
            servers = servers.filter(
                Q(name__icontains=text) | Q(host__icontains=text) | Q(location__icontains=text)
            )
        if data["status"]:
            servers = servers.filter(status=data["status"])
        return servers
