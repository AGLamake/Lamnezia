"""Peers the panel keeps on servers for itself, not for users, such as the relay probe.

The keys app does not know who owns them: reconciliation and import ask `service_peers`.
"""

from collections.abc import Callable


class ServicePeers:
    """Peers the panel puts on servers for itself, not for users: reconciliation skips them.

    The app that owns such a peer registers a lookup of their public keys by key source::

        @service_peers.register
        def relay_peers(source: str) -> set[str]: ...
    """

    def __init__(self) -> None:
        self._lookups: list[Callable[[str], set[str]]] = []

    def register(self, lookup: Callable[[str], set[str]]) -> Callable[[str], set[str]]:
        self._lookups.append(lookup)
        return lookup

    def of(self, source: str) -> set[str]:
        return set().union(*(lookup(source) for lookup in self._lookups))


service_peers = ServicePeers()
