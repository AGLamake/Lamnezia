"""What the settings page shows for a container the Amnezia app has no settings for.

IPsec keeps the fixed ports of its scripts, AmneziaDNS answers inside amnezia-dns-net, and the
app only shows the SFTP account and the onion address of the Tor site.
"""

from dataclasses import dataclass, field

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from amnezia_core.ipsec import IPSEC_CONTAINER, IPSEC_PORTS
from amnezia_core.services import DNS_ADDRESS, DNS_CONTAINER, SFTP_CONTAINER, TOR_CONTAINER
from apps.protocols.models import ProtocolInstance, ServiceInstance

_NOTES: dict[str, Promise] = {
    IPSEC_CONTAINER: _("The Amnezia app has no settings for IPsec: its ports are fixed."),
    DNS_CONTAINER: _("AmneziaDNS has no settings: the VPN containers send it the DNS requests."),
    SFTP_CONTAINER: _("As in the Amnezia app, the SFTP account is shown, not changed."),
    TOR_CONTAINER: _("As in the Amnezia app, the site in Tor has no settings."),
}


@dataclass(frozen=True)
class InfoRow:
    """One value of the page; a secret one has no value here and is sent only when asked for."""

    label: Promise
    value: str
    secret: bool = False


@dataclass(frozen=True)
class SettingsInfo:
    """A note on why there is nothing to change and the values the app shows."""

    note: Promise
    rows: list[InfoRow] = field(default_factory=list)

    @classmethod
    def of(cls, subject: ProtocolInstance | ServiceInstance) -> "SettingsInfo":
        note = _NOTES.get(subject.container, _("The panel has no settings for this container."))
        return cls(note, _rows(subject))


def _rows(subject: ProtocolInstance | ServiceInstance) -> list[InfoRow]:
    container = subject.container
    if container == IPSEC_CONTAINER:
        return [InfoRow(_("Ports"), ", ".join(f"{port}/udp" for port in IPSEC_PORTS))]
    if container == DNS_CONTAINER:
        return [InfoRow(_("Address in amnezia-dns-net"), DNS_ADDRESS)]
    if not isinstance(subject, ServiceInstance):
        return []
    if container == SFTP_CONTAINER:
        return [
            InfoRow(_("Port"), str(subject.port or "")),
            InfoRow(_("User name"), subject.login),
            # Not in the page: the button asks for it and the look is written to the journal.
            InfoRow(_("Password"), "", secret=True),
        ]
    if container == TOR_CONTAINER and subject.secret:
        return [InfoRow(_("Address in Tor"), subject.secret)]
    return []
