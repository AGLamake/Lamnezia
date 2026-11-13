"""Which servers the traffic collector polls. The apps that own servers register providers."""

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass

from apps.core.services.providers import Providers
from apps.ssh.services import RemoteHost


@dataclass(frozen=True)
class PollTarget:
    """One server and its containers with client counters, keyed by the source of each.

    `name` is the server's name, unique among the targets.
    """

    name: str
    connect: Callable[[], AbstractContextManager[RemoteHost]]
    containers: Mapping[str, str]


class PollTargets(Providers[PollTarget]):
    """Usage: ``@poll_targets.register`` on a function that yields a target per server."""

    def get(self, name: str) -> PollTarget | None:
        """The target of the server with this name, if it still has containers to poll."""
        return next((target for target in self if target.name == name), None)


poll_targets = PollTargets()
