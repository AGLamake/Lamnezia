"""Copies of the active keys of a server made from the panel, such as a relay clone.

The copy is not read from the server, so a peer the panel does not hold as an active key stays
only on the server: the copy lacks it, and its client does not work through the copy. The keys
app does not know who makes copies: the app that does registers them, and the differences page
marks the peers they lack.
"""

from collections import defaultdict

from apps.core.services.providers import Providers
from apps.keys.models import DriftKind

# Differences whose peer is on the server but not among the active keys of the panel.
LACKED_KINDS = frozenset({DriftKind.UNKNOWN_PEER, DriftKind.UNEXPECTED})


class KeyCopies(Providers[tuple[str, str]]):
    """Copies of the keys of a source, as (source, the name of the copy).

    Usage: ``@key_copies.register`` on a function that yields the pairs.
    """

    def by_source(self) -> dict[str, list[str]]:
        """The names of the copies of each source that has any, in order."""
        copies: dict[str, list[str]] = defaultdict(list)
        for source, name in self:
            copies[source].append(name)
        return {source: sorted(names) for source, names in copies.items()}


key_copies = KeyCopies()
