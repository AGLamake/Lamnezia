"""Which servers the traffic collector polls. The apps that own servers register providers."""

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

from apps.core.services.providers import Providers
from apps.ssh.services import RemoteHost


@dataclass(frozen=True)
class PollTarget:
    """One server and its containers with client counters, keyed by the source of each.

    `name` is unique among the targets: the server's name, or another one for a second target
    on the same server. `key_sources` maps the source of a clone in a server group to the source
    of the keys its peers are: those of the leading server. `interfaces` names the one interface
    of a source in a container that has several, such as the clone of a split group in the relay
    container: only its peers are read.
    """

    name: str
    connect: Callable[[], AbstractContextManager[RemoteHost]]
    containers: Mapping[str, str]
    key_sources: Mapping[str, str] = field(default_factory=dict)
    interfaces: Mapping[str, str] = field(default_factory=dict)


class PollTargets(Providers[PollTarget]):
    """Usage: ``@poll_targets.register`` on a function that yields a target per server."""

    def get(self, name: str) -> PollTarget | None:
        """The target with this name, if it still has containers to poll."""
        return next((target for target in self if target.name == name), None)


poll_targets = PollTargets()


class PeerOwners(Providers[tuple[str, str]]):
    """Peers that are not keys, as (public key, the source their traffic counts for).

    Such a peer stands on a server among its keys, but its traffic is not theirs: the uplink of
    a split group carries the traffic of every client of the group, which the clone counts.
    Usage: ``@peer_owners.register`` on a function that yields the pairs.
    """


peer_owners = PeerOwners()
