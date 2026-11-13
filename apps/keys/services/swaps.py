"""Keys reissued in a batch: new clients that take the place of old ones in one server change."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from django.utils.translation import gettext as _

from amnezia_core.clients_table import ClientRecord, ClientsTable


@dataclass(frozen=True)
class Swap:
    """A new client and the old one it replaces; `old` is empty when that one is gone already."""

    new: str
    record: ClientRecord
    old: str = ""

    def apply(self, clients: ClientsTable) -> None:
        """The clientsTable side: the new client is named, the old one leaves."""
        clients.register(self.new, self.record)
        if self.old:
            clients.remove(self.old)

    def done_line(self) -> str:
        """The log line of a batch step for a swap made on the server."""
        return _("%(name)s: reissued.") % {"name": self.record.name}

    def failed_line(self, reason: str) -> str:
        return _("%(name)s: not reissued: %(reason)s") % {
            "name": self.record.name,
            "reason": reason,
        }


@dataclass
class SwapResult[T]:
    """Per new client what it got on the server, or why its swap failed.

    A failed swap leaves the old client as it was, so running the batch again retries it.
    """

    done: dict[str, T] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)


def apply_swaps(swaps: Sequence[Swap], clients: ClientsTable) -> None:
    """A `ClientsFile.update` change for the swaps that are done."""
    for swap in swaps:
        swap.apply(clients)
