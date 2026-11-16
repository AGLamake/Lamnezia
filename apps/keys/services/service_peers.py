"""Peers the panel keeps on servers for itself, not for users, such as the relay probe.

The keys app does not know who owns them: reconciliation and import ask `service_peers`, and
the address of a new key never takes one of theirs.
"""

from collections.abc import Callable
from ipaddress import IPv4Address

# Public key → tunnel address of the service peers of a key source.
type Lookup = Callable[[str], dict[str, str]]


class ServicePeers:
    """Peers the panel puts on servers for itself, not for users: reconciliation skips them.

    The app that owns such a peer registers a lookup of their public keys and tunnel addresses
    by key source::

        @service_peers.register
        def relay_peers(source: str) -> dict[str, str]: ...
    """

    def __init__(self) -> None:
        self._lookups: list[Lookup] = []

    def register(self, lookup: Lookup) -> Lookup:
        self._lookups.append(lookup)
        return lookup

    def of(self, source: str) -> set[str]:
        """Public keys of the service peers of `source`."""
        return {public_key for found in self._found(source) for public_key in found}

    def addresses(self, source: str) -> list[IPv4Address]:
        """Tunnel addresses of the service peers of `source`: no key may take them."""
        return [IPv4Address(address) for found in self._found(source) for address in found.values()]

    def _found(self, source: str) -> list[dict[str, str]]:
        return [lookup(source) for lookup in self._lookups]


service_peers = ServicePeers()
