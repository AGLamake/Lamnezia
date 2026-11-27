"""Network lists of split routing: the formats of their sources, own entries and the merge.

A split group in the blacklist mode sends the networks of its list straight out of the relay and
the rest over the uplink; in the whitelist mode it sends the networks of its list over the uplink
and the rest straight out (docs/SPLIT.md 4.5). The panel downloads the sources, reads them here
and merges them with the administrator's own entries: networks and domains that always go
through the relay (`local`) or always abroad (`abroad`). The relay agent resolves the domains
itself.

A source is a CIDR text (one network per line), the JSON of the Amnezia application's site list
(`lib4u`, an array of `{hostname, ip, ips}`, read as `ipSplitTunnelingController.cpp` reads it)
or the delegation statistics of a regional registry (`ripe`). IPv6 is skipped: the relay routes
IPv4 only.
"""

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Network,
    collapse_addresses,
    ip_network,
    summarize_address_range,
)

from amnezia_core.errors import AmneziaFormatError

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_DOMAIN_LENGTH = 253
# The fields of a record of the delegation statistics: registry|cc|type|start|value|date|status.
_RIPE_FIELDS = 7
_RIPE_STATUSES = frozenset({"allocated", "assigned"})


class SplitListError(AmneziaFormatError):
    """A source list or an own entry cannot be read."""


class SourceFormat(StrEnum):
    CIDR = "cidr"
    LIB4U = "lib4u"
    RIPE = "ripe"

    def parse(self, text: str) -> tuple[IPv4Network, ...]:
        """The IPv4 networks of the list, merged; SplitListError if it is broken or empty."""
        reader = {
            SourceFormat.CIDR: _cidr_networks,
            SourceFormat.LIB4U: _lib4u_networks,
            SourceFormat.RIPE: _ripe_networks,
        }[self]
        networks = tuple(collapse_addresses(reader(text)))
        if not networks:
            raise SplitListError("The list has no IPv4 network.")
        return networks


def _network(value: str) -> IPv4Network | IPv6Network:
    """A network or a single address; host bits are dropped, as lists often keep them."""
    return ip_network(value.strip(), strict=False)


def _cidr_networks(text: str) -> Iterator[IPv4Network]:
    for number, line in enumerate(text.splitlines(), start=1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        try:
            network = _network(entry)
        except ValueError:
            raise SplitListError(f"Line {number} is not a network.") from None
        if isinstance(network, IPv4Network):
            yield network


def _lib4u_networks(text: str) -> Iterator[IPv4Network]:
    """The hostname when it is a network, otherwise its addresses; a domain without addresses
    is skipped, as the panel resolves no domains."""
    try:
        sites = json.loads(text)
    except ValueError:
        raise SplitListError("The list is not JSON.") from None
    if not isinstance(sites, list):
        raise SplitListError("The list is not a JSON array.")
    for site in sites:
        if not isinstance(site, dict):
            continue
        hostname = _ipv4(site.get("hostname"))
        if hostname is not None:
            yield hostname
            continue
        addresses = site.get("ips")
        values = [*addresses] if isinstance(addresses, list) else []
        values.append(site.get("ip"))
        yield from (found for found in map(_ipv4, values) if found is not None)


def _ipv4(value: object) -> IPv4Network | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        network = _network(value)
    except ValueError:
        return None
    return network if isinstance(network, IPv4Network) else None


def _ripe_networks(text: str, country: str = "RU") -> Iterator[IPv4Network]:
    """IPv4 blocks allocated or assigned to the country. A block is a start address and a
    number of addresses, not always a power of two: it may take several networks."""
    for number, line in enumerate(text.splitlines(), start=1):
        fields = line.strip().split("|")
        if len(fields) < _RIPE_FIELDS or fields[1] != country or fields[2] != "ipv4":
            continue
        if fields[6] not in _RIPE_STATUSES:
            continue
        try:
            first = IPv4Address(fields[3])
            last = IPv4Address(int(first) + int(fields[4]) - 1)
            yield from summarize_address_range(first, last)
        except ValueError:
            raise SplitListError(f"Line {number} is not an IPv4 block.") from None


class Direction(StrEnum):
    """Where an own entry goes: through the relay straight out, or abroad over the uplink."""

    LOCAL = "local"
    ABROAD = "abroad"


@dataclass(frozen=True)
class Target:
    """An own entry: a network or a domain, as the administrator types it."""

    network: IPv4Network | None = None
    domain: str = ""

    @classmethod
    def parse(cls, value: str) -> "Target":
        """An IPv4 network or address, otherwise a domain name in lower case and punycode."""
        value = value.strip()
        try:
            network = _network(value)
        except ValueError:
            return cls(domain=_domain(value))
        if not isinstance(network, IPv4Network):
            raise SplitListError("Only IPv4 networks are routed.")
        return cls(network=network)

    def __str__(self) -> str:
        return str(self.network) if self.network is not None else self.domain


def _domain(value: str) -> str:
    try:
        name = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise SplitListError("Not a network or a domain name.") from None
    labels = name.split(".")
    valid = len(labels) > 1 and all(_LABEL.match(label) for label in labels)
    if not valid or len(name) > _DOMAIN_LENGTH or labels[-1].isdigit():
        raise SplitListError("Not a network or a domain name.")
    return name


class ListMode(StrEnum):
    """What the list of a split group holds: the networks sent straight out (`exclude`, a
    blacklist of the uplink) or the only networks sent abroad (`include`, a whitelist)."""

    EXCLUDE = "exclude"
    INCLUDE = "include"


def listed_networks(
    mode: ListMode,
    sources: Iterable[IPv4Network],
    local: Iterable[IPv4Network],
    abroad: Iterable[IPv4Network],
) -> tuple[IPv4Network, ...]:
    """The merged list a split group routes by, with the own networks over the sources.

    In the blacklist mode it is the networks sent straight out: the sources and the own `local`
    networks without the own `abroad` ones. In the whitelist mode it is the networks sent
    abroad: the sources without the own `local` networks, and the own `abroad` ones. An abroad
    entry wins over a local one in both modes.
    """
    if mode == ListMode.EXCLUDE:
        return _subtract([*sources, *local], abroad)
    return tuple(collapse_addresses([*_subtract(sources, local), *abroad]))


def _subtract(
    networks: Iterable[IPv4Network], excluded: Iterable[IPv4Network]
) -> tuple[IPv4Network, ...]:
    kept = list(collapse_addresses(networks))
    for gone in collapse_addresses(excluded):
        kept = [part for network in kept for part in _without(network, gone)]
    return tuple(collapse_addresses(kept))


def _without(network: IPv4Network, excluded: IPv4Network) -> Iterable[IPv4Network]:
    if not network.overlaps(excluded):
        return (network,)
    if network.subnet_of(excluded):
        return ()
    return network.address_exclude(excluded)
