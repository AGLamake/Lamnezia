"""The choice of the I1 template on the install, upgrade and settings forms of AmneziaWG."""

from django import forms
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from amnezia_core.awg_masking import Masking

# A template drawn at random for each server; the default of a new server.
RANDOM = "random"
TEMPLATES: tuple[tuple[str, Promise], ...] = (
    (RANDOM, _("Random template")),
    (Masking.STUN, _("STUN: a WebRTC call")),
    (Masking.QUIC, _("QUIC: an HTTP/3 connection")),
    (Masking.DNS, _("DNS query")),
    (Masking.SIP, _("SIP: a phone call")),
    (Masking.ICLOUD, _("DNS answer for icloud.com, as in the app, with an Apple address")),
)
_HELP = _(
    "The first packet a client sends, I1, looks like the start of this protocol. The Amnezia app "
    "sends the same packet from every server, so it is easy to recognise."
)


def masking_field(*, keep: bool = False) -> forms.ChoiceField:
    """The template choice; with `keep` the first choice leaves the current I1 as it is."""
    choices = (("", _("Keep the current I1")), *TEMPLATES) if keep else TEMPLATES
    return forms.ChoiceField(
        label=_("I1 masking"),
        choices=choices,
        required=False,
        initial="" if keep else RANDOM,
        help_text=_HELP,
    )


def masking_packet(value: str) -> str:
    """A new I1 for a choice; the random choice draws its template here."""
    return (Masking.named(value) or Masking.pick()).packet()
