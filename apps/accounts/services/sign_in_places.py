"""The networks and browsers a user signs in from: whether a sign-in comes from a new place.

The address itself is not kept. Its network (/24 for IPv4, /64 for IPv6, so a new lease from
the same provider is not news) is stored as a keyed hash, which only tells a known one apart.
"""

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.accounts.models import KnownSignIn, User

_PREFIX = {4: 24, 6: 64}


@dataclass(frozen=True)
class NewPlace:
    """A sign-in from a network or a browser the user has not signed in from before."""

    address: str
    device: str
    new_network: bool
    new_device: bool


class SignInPlaces:
    """The places one user signed in from; a place not seen for `FORGET_AFTER` is forgotten."""

    FORGET_AFTER = timedelta(days=180)

    def __init__(self, user: User) -> None:
        self._user = user

    def visit(self, address: str | None, device: str) -> NewPlace | None:
        """Remember the place of this sign-in; what is new about it, or None when nothing is.

        The very first sign-in known for the user is not news: there is nothing to compare with.
        An unknown address leaves the network out of the comparison.
        """
        now = timezone.now()
        known = self._user.known_sign_ins
        known.filter(last_seen__lt=now - self.FORGET_AFTER).delete()
        network = network_hash(address)
        first = not known.exists()
        new_network = bool(network) and not known.filter(network=network).exists()
        new_device = not known.filter(device=device).exists()
        KnownSignIn.objects.update_or_create(
            user=self._user, network=network, device=device, defaults={"last_seen": now}
        )
        if first or not (new_network or new_device):
            return None
        return NewPlace(address or "", device, new_network, new_device)


def network_hash(address: str | None) -> str:
    """The keyed hash of the network of `address`; empty for an unknown address."""
    try:
        ip = ipaddress.ip_address(address or "")
    except ValueError:
        return ""
    network = ipaddress.ip_network(f"{ip}/{_PREFIX[ip.version]}", strict=False)
    key = f"{settings.SECRET_KEY}:sign-in-network".encode()
    return hmac.new(key, str(network).encode(), hashlib.sha256).hexdigest()
