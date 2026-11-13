"""Targets the user's browser probes to see whether their servers are reachable from its network.

Only servers the user already has a key on are given out: their address is in the key anyway.
The browser keeps the result to itself; the panel never learns or stores it.

UDP is checked with STUN against the probe on the server (`lamnezia-probe`), TCP with a request
to the port of an XRay (Reality) instance itself; see docs/DESIGN.md, 3.9.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv6Address, ip_address

from amnezia_core.xray import XRAY_CONTAINER
from apps.keys.services.instances import Instance
from apps.servers.models import Server

# Ports browsers never fetch (Fetch Standard, "bad port"): a request fails at once, as if the
# server answered, so such a port tells nothing.
BAD_PORTS = frozenset(
    {
        0, 1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79, 87,
        95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137, 139, 143,
        161, 179, 389, 427, 465, 512, 513, 514, 515, 526, 530, 531, 532, 540, 548, 554, 556,
        563, 587, 601, 636, 989, 990, 993, 995, 1719, 1720, 1723, 2049, 3659, 4045, 4190,
        5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669, 6679, 6697, 10080,
    }
)  # fmt: skip
# A host a Content-Security-Policy source may name: a domain or an IPv4 address. CSP has no
# syntax for IPv6 addresses, so the page could not allow the request to one.
_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
_CSP_HOST = re.compile(rf"^{_LABEL}(\.{_LABEL})*$")


@dataclass(frozen=True)
class ProbeTarget:
    """`url` is `stun:host:port` for UDP or `https://host:port/` for TCP."""

    server: str
    url: str
    server_id: int | None = None

    @property
    def origin(self) -> str | None:
        """What the page's CSP must allow for a TCP check; None for STUN, which CSP ignores."""
        return self.url.removesuffix("/") if self.url.startswith("https://") else None


@dataclass(frozen=True)
class ServerProbe:
    """Every address of one server the browser checks; the page warns once if any gets no answer."""

    server: str
    server_id: int | None
    urls: tuple[str, ...]


@dataclass(frozen=True)
class Probes:
    """The checks of a page: the targets its CSP must allow and one warning per server."""

    targets: tuple[ProbeTarget, ...] = ()

    @classmethod
    def of(cls, keyed: Iterable[Instance]) -> "Probes":
        return cls(tuple(probe_targets(keyed)))

    def __bool__(self) -> bool:
        return bool(self.targets)

    @property
    def origins(self) -> list[str]:
        return [target.origin for target in self.targets if target.origin]

    @property
    def servers(self) -> list[ServerProbe]:
        """A server with several addresses or an XRay port next to its probe is one warning."""
        urls: dict[tuple[str, int | None], list[str]] = {}
        for target in self.targets:
            urls.setdefault((target.server, target.server_id), []).append(target.url)
        return [
            ServerProbe(name, server_id, tuple(found))
            for (name, server_id), found in sorted(urls.items(), key=lambda item: item[0][0])
        ]


def probe_targets(keyed: Iterable[Instance]) -> list[ProbeTarget]:
    """UDP targets per address on servers with a running probe, then TCP ones per XRay port."""
    keyed = list(keyed)
    servers = {
        server.pk: server
        for server in Server.objects.filter(
            pk__in={i.server_id for i in keyed if i.server_id is not None}
        ).select_related("latest_sample")
    }
    return [*_udp_targets(keyed, servers), *_tcp_targets(keyed, servers)]


def _udp_targets(keyed: list[Instance], servers: dict[int, Server]) -> list[ProbeTarget]:
    hosts: dict[int, set[str]] = {}
    for instance in keyed:
        if instance.server_id in servers:
            hosts.setdefault(instance.server_id, set()).add(instance.host)
    return [
        ProbeTarget(server.name, _stun_url(host, server.probe_port), server.pk)
        for server in sorted(servers.values(), key=lambda server: server.name)
        if server.probe_port is not None and not server.probe_down
        for host in sorted(hosts.get(server.pk, ()))
    ]


def _tcp_targets(keyed: list[Instance], servers: dict[int, Server]) -> list[ProbeTarget]:
    targets = {
        ProbeTarget(server.name, f"https://{instance.host}:{instance.port}/", server.pk)
        for instance in keyed
        if instance.container.name == XRAY_CONTAINER
        and (server := servers.get(instance.server_id or 0)) is not None
        and instance.port is not None
        and instance.port not in BAD_PORTS
        and _CSP_HOST.match(instance.host)
    }
    return sorted(targets, key=lambda target: (target.server, target.url))


def _stun_url(host: str, port: int) -> str:
    try:
        ipv6 = isinstance(ip_address(host), IPv6Address)
    except ValueError:
        ipv6 = False
    return f"stun:[{host}]:{port}" if ipv6 else f"stun:{host}:{port}"
