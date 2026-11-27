"""Agent of the fallback relay: probes the members of each group and moves the port to a live one.

docs/FALLBACK.md section 5. It runs in the relay container on its own and needs only the Python
standard library: the panel writes `config.json` next to it, the agent re-reads it when it
changes and writes `state.json`, which the panel reads over SSH. For planned maintenance the
panel may put `switch.json` next to them: the agent takes it once, moves the group to the asked
member only if it answers a probe and writes the answer into the state. The file formats are
those of `amnezia_core.relay`.

Each group has one service interface `lr<port>`; a probe moves it to a new random source port,
re-adds the server peer with the member's address and waits for a new handshake. The new port
makes each probe a new flow, like a client that connects: a filter on the way may pass a flow
it already knows and drop every new one. Forwarding is nftables DNAT plus masquerade, replaced
in one `nft -f` transaction; the conntrack entries of the group port are dropped right after a
switch, or the clients would stay on the old member.

The agent also reads the conntrack entries of the clients: when several clients reach the
active member and none gets an answer, round after round, the state says so. It does not switch
on that: clients with keys the servers no longer know go unanswered by every member.

A group in split routing (docs/SPLIT.md) is not forwarded: a clone of the group `lg<port>`
listens on its port, packets to private networks leave the relay directly, and the list mode
of the group decides the rest. In the blacklist mode packets to the networks of its list leave
directly and the rest gets a firewall mark and goes over the uplink `lu<port>`, a client of the
group on the active member; in the whitelist mode only packets to the networks of its list get
the mark, and a DNS query the relay's resolver does not answer goes over the uplink too. A new
active member only changes the uplink's endpoint, so the clients' sessions stay. When the uplink
fails on every live member, the group falls back to forwarding (`Lane`). The own domains of a
split group are resolved here, in the background every few minutes (`Domains`): their addresses
go straight out or always over the uplink. The names and addresses are not logged.

The clients of a split group ask its keys' DNS addresses; the relay answers those queries with
its own resolver (`Resolver`), which asks 1.1.1.1 over DNS-over-TLS straight from the relay: the
answers are those for Russia, and the network does not see the names. The resolver writes no
log of queries or answers, and runs only while some group is split.

The speed limits of the keys of a split group are a tc script the panel writes next to the
clone peers; the agent runs it on the clone after every change of the clone or of the script.
"""

import functools
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

FORMAT_VERSION = 1
FOLDER = Path("/opt/amnezia/relay")
TABLE = "lamnezia_relay"
# A live member answers in 0.1-0.3 s, a blocked one not in 3 s (docs/research/relay-fallback.md).
PROBE_TIMEOUT = 1.0
_PROBE_POLL = 0.1
# Seconds a ping through the uplink waits for its answer.
UPLINK_TIMEOUT = 1
# Seconds the uplink must go unanswered before it leaves its member: the way abroad may drop
# everything for a few seconds now and then on every member alike.
UPLINK_SILENCE = 30
# Unanswered clients needed to call the active member silent: one is likely a deleted key.
SILENT_CLIENTS = 2
DIRECTIONS = 2  # a conntrack entry lists the original and the reply direction

# Defaults of `amnezia_core.relay` for a config of an older panel.
DEFAULT_RECOVERY = 6
DEFAULT_UPLINK_CHECK = "1.1.1.1"

# Routing of a group (`amnezia_core.relay.Routing`), also the mode a split group is in.
FORWARD = "forward"
SPLIT = "split"
UPLINK_KINDS = ("awg",)
# List modes of a split group (`amnezia_core.split_lists.ListMode`).
EXCLUDE = "exclude"
INCLUDE = "include"
# Firewall mark and route table of a split group: the base plus the port, clear of the main ones.
MARK_BASE = 0x10000
# Never sent over the uplink: private, loopback, link-local and multicast networks.
LOCAL_NETWORKS = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/3",
)  # fmt: skip

# Seconds between lookups of the own domains of split groups, and lookups at a time.
RESOLVE_INTERVAL = 600
RESOLVE_WORKERS = 8

# The resolver of the split groups' clients: unbound asking Cloudflare over DNS-over-TLS.
RESOLVER_CONFIG = "resolver.conf"
RESOLVER_UPSTREAMS = ("1.1.1.1@853#cloudflare-dns.com", "1.0.0.1@853#cloudflare-dns.com")
RESOLVER_CERTIFICATES = "/etc/ssl/certs/ca-certificates.crt"
# Seconds before a resolver that did not start or stopped is started again.
RESOLVER_RETRY = 60
RESOLVER_STOP_TIMEOUT = 5
DNS_PORT = 53
# Directions of an own domain (`amnezia_core.split_lists.Direction`).
LOCAL = "local"
ABROAD = "abroad"

# Answers to a switch request of the panel (`amnezia_core.relay.SwitchOutcome`).
SWITCHED = "switched"
ALREADY_ACTIVE = "already_active"
UNREACHABLE = "unreachable"
UNKNOWN_GROUP = "unknown_group"
UNKNOWN_BACKEND = "unknown_backend"


@dataclass(frozen=True)
class Backend:
    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port}

    @classmethod
    def from_dict(cls, data: Any) -> "Backend":
        if not isinstance(data, dict):
            raise ValueError("a backend must be an object")
        return cls(str(data["host"]), int(data["port"]))


@dataclass(frozen=True)
class Split:
    """The clone and the uplink peer of a split group (`amnezia_core.relay.SplitRouting`)."""

    clone: Mapping[str, Any]
    uplink: Mapping[str, Any]
    kind: str = "awg"
    dns: tuple[str, ...] = ()
    list_mode: str = EXCLUDE

    @property
    def subnet(self) -> str:
        return str(ipaddress.IPv4Interface(self.clone["address"]).network)

    @classmethod
    def from_dict(cls, data: Any) -> "Split":
        if not isinstance(data, dict):
            raise ValueError("split must be an object")
        dns = tuple(str(ipaddress.IPv4Address(address)) for address in data.get("dns", []))
        split = cls(
            dict(data["clone"]),
            dict(data["uplink"]),
            str(data.get("kind", "awg")),
            dns,
            str(data.get("list_mode", EXCLUDE)),
        )
        if split.kind not in UPLINK_KINDS:
            raise ValueError(f"unknown uplink kind {split.kind!r}")
        if split.list_mode not in (EXCLUDE, INCLUDE):
            raise ValueError(f"unknown list mode {split.list_mode!r}")
        ipaddress.IPv4Interface(split.clone["address"])
        ipaddress.IPv4Interface(split.uplink["address"])
        return split


@dataclass(frozen=True)
class Group:
    """A group from the config: its port, the members by priority, the service peer and, for
    a split group, its clone and uplink."""

    port: int
    backends: tuple[Backend, ...]
    service: Mapping[str, Any]
    split: Split | None = None

    @property
    def interface(self) -> str:
        return f"lr{self.port}"

    @property
    def clone_interface(self) -> str:
        return f"lg{self.port}"

    @property
    def uplink_interface(self) -> str:
        return f"lu{self.port}"

    @property
    def mark(self) -> int:
        return MARK_BASE + self.port


@dataclass(frozen=True)
class Settings:
    groups: tuple[Group, ...]
    check_interval: int
    failures: int
    recovery: int = DEFAULT_RECOVERY
    uplink_check: str = DEFAULT_UPLINK_CHECK

    @classmethod
    def parse(cls, text: str) -> "Settings":
        """The config the panel wrote; ValueError if it is not one this agent knows."""
        try:
            data = json.loads(text)
            if data.get("version") != FORMAT_VERSION:
                raise ValueError(f"unknown config version {data.get('version')!r}")
            check = ipaddress.IPv4Address(data.get("uplink_check", DEFAULT_UPLINK_CHECK))
            return cls(
                groups=tuple(_group(item) for item in data["groups"]),
                check_interval=int(data["check_interval"]),
                failures=int(data["failures"]),
                recovery=int(data.get("recovery", DEFAULT_RECOVERY)),
                uplink_check=str(check),
            )
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError(f"bad config: {error!r}") from error


def _group(item: Mapping[str, Any]) -> Group:
    routing = item.get("routing", FORWARD)
    if routing not in (FORWARD, SPLIT):
        raise ValueError(f"unknown routing {routing!r}")
    return Group(
        int(item["port"]),
        tuple(Backend.from_dict(backend) for backend in item["backends"]),
        dict(item["service"]),
        Split.from_dict(item["split"]) if routing == SPLIT else None,
    )


@dataclass(frozen=True)
class Flow:
    """A conntrack entry of a client: the member it was sent to and whether that one answered."""

    client: str
    member: str
    answered: bool


@dataclass
class Health:
    alive: bool = False
    failures: int = 0
    checked_at: int | None = None

    def record(self, alive: bool, now: int) -> None:
        self.alive, self.checked_at = alive, now
        self.failures = 0 if alive else self.failures + 1


class Watch:
    """The sticky choice of one group: stay on the active member while it lives.

    After `limit` failed probes in a row the group moves to the first member by priority that
    answered the probe of this same round. It is not probed once more: a member drops a second
    handshake that comes within milliseconds of the first, and on a short link the switch would
    then never happen. A member of higher priority that comes back does not take the group over:
    every switch drops the sessions of all clients.
    """

    def __init__(self, group: Group, limit: int, active: Backend | None = None) -> None:
        self.group = group
        self.limit = limit
        self.active = active
        self.switched_at: int | None = None
        self.health: dict[Backend, Health] = {}
        # Rounds in a row in which clients reached the active member and none got an answer.
        self.silent = 0
        self.update(group, limit)

    def update(self, group: Group, limit: int) -> None:
        """Take a new member list; what is known of the members that stay is kept."""
        self.group, self.limit = group, limit
        self.health = {backend: self.health.get(backend, Health()) for backend in group.backends}

    @property
    def live(self) -> list[Backend]:
        """Members that answered their last probe, by priority."""
        return [backend for backend in self.group.backends if self.health[backend].alive]

    def check(self, probe: Callable[[Backend], bool], now: int) -> bool:
        """Probe every member; True if the group moved to another one."""
        for backend in self.group.backends:
            self.health[backend].record(probe(backend), now)
        if self.active in self.health and self.health[self.active].failures < self.limit:
            return False
        for candidate in self.live:
            if candidate != self.active:
                self.move(candidate, now)
                return True
        return False

    def observe(self, flows: Iterable[Flow]) -> None:
        """Count the round as silent when `SILENT_CLIENTS` clients wait for the active member
        and it answers none of its clients."""
        if self.active is None:
            self.silent = 0
            return
        to_active = [flow for flow in flows if flow.member == self.active.host]
        waiting = {flow.client for flow in to_active if not flow.answered}
        answered = any(flow.answered for flow in to_active)
        silent = len(waiting) >= SILENT_CLIENTS and not answered
        self.silent = self.silent + 1 if silent else 0

    def switch_to(self, backend: Backend, probe: Callable[[Backend], bool], now: int) -> str:
        """A switch the panel asked for: taken only if the member answers a probe right now."""
        if backend not in self.health:
            return UNKNOWN_BACKEND
        if backend == self.active:
            return ALREADY_ACTIVE
        alive = probe(backend)
        self.health[backend].record(alive, now)
        if not alive:
            return UNREACHABLE
        self.move(backend, now)
        return SWITCHED

    def move(self, backend: Backend, now: int) -> None:
        self.active, self.switched_at, self.silent = backend, now, 0

    def state(self, traffic: tuple[int, int]) -> dict[str, Any]:
        return {
            "port": self.group.port,
            "active": self.active.to_dict() if self.active else None,
            "switched_at": self.switched_at,
            "unanswered": self.silent >= self.limit,
            "backends": [
                {**backend.to_dict(), **vars(health)} for backend, health in self.health.items()
            ],
            "bytes_from_clients": traffic[0],
            "bytes_to_clients": traffic[1],
        }


class Lane:
    """The way out of a split group: the uplink to a member, or the emergency forward.

    docs/SPLIT.md 4.3. The uplink points at the active member of the group. After `limit`
    failed checks in a row, which span at least `UPLINK_SILENCE` seconds, it moves to the next
    member that answers probes and has not failed the uplink yet. When none is left but a member
    still answers a probe, the group falls back to forwarding its port to that member; when no
    member answers at all it stays split, so Russian sites still work. In the forward fallback
    the uplink is checked on: after a failure it tries the next live member, and `recovery` good
    checks in a row split the group again. A failed member is forgotten only after `recovery`
    good checks in a row too: a lossy way out on every member does not bounce between them.
    """

    POINT = "point"
    REROUTE = "reroute"

    def __init__(self, limit: int, recovery: int, mode: str = SPLIT) -> None:
        self.limit, self.recovery, self.mode = limit, recovery, mode
        self.changed_at: int | None = None
        self.endpoint: Backend | None = None
        self.health = Health()
        self.successes = 0
        # When the failed checks in a row began; None after a good one.
        self.failing_since: int | None = None
        # Members whose uplink failed since the last `recovery` good checks in a row.
        self.failed: set[Backend] = set()

    @property
    def split(self) -> bool:
        return self.mode == SPLIT

    def update(self, limit: int, recovery: int) -> None:
        self.limit, self.recovery = limit, recovery

    def point(self, backend: Backend) -> None:
        self.endpoint = backend
        self._restart()

    def record(self, alive: bool, watch: Watch, now: int) -> str | None:
        """Count one check of the uplink: POINT when it must go to `endpoint`, REROUTE when the
        mode changed and with it the group's rules."""
        self.health.record(alive, now)
        if alive:
            self.failing_since = None
            return self._succeed(watch, now)
        self.successes = 0
        if self.failing_since is None:
            self.failing_since = now
        if not self.split:
            return self._try_next(watch)
        if self.health.failures < self.limit or now - self.failing_since < UPLINK_SILENCE:
            return None
        return self._leave(watch, now)

    def state(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint.to_dict() if self.endpoint else None,
            "alive": self.health.alive,
            "checked_at": self.health.checked_at,
        }

    def _succeed(self, watch: Watch, now: int) -> str | None:
        """A good check; in the forward fallback the last of `recovery` splits the group."""
        self.successes += 1
        if self.successes >= self.recovery:
            self.failed.clear()
        if self.split or self.successes < self.recovery or self.endpoint is None:
            return None
        if watch.active != self.endpoint:
            watch.move(self.endpoint, now)
        self._change(SPLIT, now)
        return self.REROUTE

    def _leave(self, watch: Watch, now: int) -> str | None:
        """The uplink failed `limit` times: the next member, or the forward fallback."""
        if self.endpoint is not None:
            self.failed.add(self.endpoint)
        live = watch.live
        candidate = next((backend for backend in live if backend not in self.failed), None)
        if candidate is not None:
            watch.move(candidate, now)
            self.point(candidate)
            return self.POINT
        self.failed.clear()
        if not live:
            return None
        if watch.active not in live:
            watch.move(live[0], now)
        self._change(FORWARD, now)
        return self.REROUTE

    def _try_next(self, watch: Watch) -> str | None:
        """In the forward fallback, point the uplink at the live member after the current one."""
        live = watch.live
        if not live:
            return None
        after = live.index(self.endpoint) + 1 if self.endpoint in live else 0
        candidate = live[after % len(live)]
        if candidate == self.endpoint:
            return None
        self.point(candidate)
        return self.POINT

    def _change(self, mode: str, now: int) -> None:
        self.mode, self.changed_at = mode, now
        self._restart()

    def _restart(self) -> None:
        """Count the checks anew: the uplink goes another way."""
        self.health.failures = self.successes = 0
        self.failing_since = None


@dataclass(frozen=True)
class SplitRoute:
    """The nftables part of a split group: its clone's subnet, the networks of its list (sent
    directly in the blacklist mode, the only ones over the uplink in the whitelist mode), the
    addresses of its own domains, sent directly (`local`) or always over the uplink (`abroad`),
    and the DNS addresses whose queries the relay's resolver answers."""

    port: int
    subnet: str
    networks: tuple[str, ...] = ()
    local: tuple[str, ...] = ()
    abroad: tuple[str, ...] = ()
    dns: tuple[str, ...] = ()
    list_mode: str = EXCLUDE


def ruleset(routes: Mapping[int, Backend | None], splits: Sequence[SplitRoute] = ()) -> str:
    """The whole nftables table for one `nft -f`: the active member of each forwarded port and
    the marks of each split group.

    Declaring and deleting the table first replaces it atomically, with no moment without rules,
    and a new network list with it. Masquerade keeps the relay in the path: without it the
    member answers the client directly. A packet from a split group's clone to an address out of
    the private networks and its local domains gets the group's mark, which routes it into the
    uplink, when the address is out of its list in the blacklist mode, or in its list in the
    whitelist mode; so does one to an address of its abroad domains, whatever the list says. A
    DNS query from the clone to one of the group's DNS addresses is redirected to the relay's
    resolver; the local routing table comes before the mark, so the query stays on the relay.
    In the whitelist mode any other DNS query is marked: plain DNS straight out of the relay
    would show the names to the Russian network and take its answers.
    """
    ports = sorted({*routes, *(split.port for split in splits)})
    counters = [
        line
        for port in ports
        for line in (f"\tcounter from_clients_{port} {{ }}", f"\tcounter to_clients_{port} {{ }}")
    ]
    sets: list[str] = _set("local", LOCAL_NETWORKS) if splits else []
    dnat: list[str] = []
    marks: list[str] = []
    masquerade: list[str] = []
    count: list[str] = []
    for port, backend in sorted(routes.items()):
        if backend is None:
            continue
        member = f"ip daddr {backend.host} udp dport {backend.port}"
        dnat.append(f"\t\tudp dport {port} dnat to {backend}")
        masquerade.append(f"\t\t{member} masquerade")
        count.append(f'\t\t{member} counter name "from_clients_{port}"')
        count.append(
            f'\t\tip saddr {backend.host} udp sport {backend.port} counter name "to_clients_{port}"'
        )
    for split in sorted(splits, key=lambda item: item.port):
        port, clone, uplink = split.port, f"lg{split.port}", f"lu{split.port}"
        if split.dns:
            servers = "{ " + ", ".join(split.dns) + " }"
            dnat += [
                f'\t\tiifname "{clone}" ip daddr {servers} {proto} dport {DNS_PORT} '
                f"redirect to :{DNS_PORT}"
                for proto in ("udp", "tcp")
            ]
        sets += _set(f"ru_{port}", split.networks)
        sets += _set(f"dl_{port}", split.local)
        sets += _set(f"da_{port}", split.abroad)
        mark = f"meta mark set {MARK_BASE + port}"
        listed = "" if split.list_mode == INCLUDE else "!= "
        marks.append(f'\t\tiifname "{clone}" ip daddr != @local ip daddr @da_{port} {mark}')
        marks.append(
            f'\t\tiifname "{clone}" ip daddr != @local ip daddr {listed}@ru_{port} '
            f"ip daddr != @dl_{port} {mark}"
        )
        if split.list_mode == INCLUDE:
            marks += [
                f'\t\tiifname "{clone}" ip daddr != @local {proto} dport {DNS_PORT} {mark}'
                for proto in ("udp", "tcp")
            ]
        masquerade.append(f'\t\tip saddr {split.subnet} oifname != "{clone}" masquerade')
        count.append(f'\t\tiifname "{clone}" counter name "from_clients_{port}"')
        count.append(f'\t\toifname "{clone}" counter name "to_clients_{port}"')
        count.append(f'\t\toifname "{uplink}" tcp flags syn tcp option maxseg size set rt mtu')
    lines = [
        f"table ip {TABLE}",
        f"delete table ip {TABLE}",
        f"table ip {TABLE} {{",
        *counters,
        *sets,
        "\tchain prerouting {",
        "\t\ttype nat hook prerouting priority dstnat; policy accept;",
        *dnat,
        "\t}",
        "\tchain steer {",
        "\t\ttype filter hook prerouting priority mangle; policy accept;",
        *marks,
        "\t}",
        "\tchain postrouting {",
        "\t\ttype nat hook postrouting priority srcnat; policy accept;",
        *masquerade,
        "\t}",
        "\tchain forward {",
        "\t\ttype filter hook forward priority filter; policy accept;",
        *count,
        "\t}",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _set(name: str, networks: Sequence[str]) -> list[str]:
    lines = [f"\tset {name} {{", "\t\ttype ipv4_addr", "\t\tflags interval", "\t\tauto-merge"]
    if networks:
        lines.append("\t\telements = { " + ",\n\t\t\t".join(networks) + " }")
    return [*lines, "\t}"]


def parse_networks(text: str) -> tuple[str, ...]:
    """The network list of a split group, merged; ValueError if it is empty or a line is bad.

    One network per line, `#` starts a comment (`amnezia_core.relay.NetworkList`).
    """
    networks = []
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            networks.append(ipaddress.IPv4Network(entry))
    if not networks:
        raise ValueError("the list is empty")
    return tuple(str(network) for network in ipaddress.collapse_addresses(networks))


class Network(Protocol):
    """What the agent does to the container network; `Shell` runs the real commands."""

    def prepare(self, group: Group) -> None:
        """Bring up the service interface of the group with its service peer keys."""

    def drop(self, port: int) -> None:
        """Remove every interface of a group that left the config."""

    def probe(self, group: Group, backend: Backend) -> bool:
        """A new handshake of the service peer with the member, from a new source port."""
        ...

    def flows(self, port: int) -> list[Flow]:
        """The conntrack entries of the clients of a group port."""
        ...

    def forward(self, rules: str) -> None:
        """Replace the forwarding rules in one transaction."""

    def forget(self, port: int) -> None:
        """Drop the conntrack entries of the group port: its clients follow the new rule."""

    def counters(self) -> dict[int, tuple[int, int]]:
        """Bytes from and to the clients of each port since the rules were last replaced."""
        ...

    def clone(self, group: Group) -> None:
        """Bring up or refresh the clone and the uplink of a split group and route its marked
        packets into the uplink; the sessions of the clone's clients stay."""

    def unclone(self, port: int) -> None:
        """Remove the clone, the uplink and their route: the group is forwarded again."""

    def limit(self, script: Path) -> None:
        """Run the tc script with the speed limits of a clone."""

    def point(self, group: Group, backend: Backend) -> None:
        """Point the uplink at the member; only its endpoint changes."""

    def uplink_alive(self, group: Group, target: str) -> bool:
        """Whether the target answers through the uplink now."""
        ...


@dataclass
class Traffic:
    """Bytes of each port, kept across rule replacements, which reset the nft counters."""

    base: dict[int, tuple[int, int]] = field(default_factory=dict)

    def keep(self, current: Mapping[int, tuple[int, int]]) -> None:
        for port, counted in current.items():
            self.base[port] = _sum(self.base.get(port, (0, 0)), counted)

    def total(self, port: int, current: Mapping[int, tuple[int, int]]) -> tuple[int, int]:
        return _sum(self.base.get(port, (0, 0)), current.get(port, (0, 0)))


def _sum(left: tuple[int, int], right: tuple[int, int]) -> tuple[int, int]:
    return left[0] + right[0], left[1] + right[1]


def parse_domains(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The local and the abroad domains of a split group; ValueError if a line is bad.

    One domain per line after its direction (`amnezia_core.relay.DomainList`).
    """
    found: dict[str, set[str]] = {LOCAL: set(), ABROAD: set()}
    for number, line in enumerate(text.splitlines(), start=1):
        entry = line.split("#", 1)[0].split()
        if not entry:
            continue
        if len(entry) != 2 or entry[0] not in found:  # noqa: PLR2004 - direction and name
            raise ValueError(f"line {number} is not a direction and a domain")
        found[entry[0]].add(entry[1])
    return tuple(sorted(found[LOCAL])), tuple(sorted(found[ABROAD]))


def resolve(name: str) -> tuple[str, ...]:
    """The IPv4 addresses of a name, by the resolver of the relay."""
    found = socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)
    return tuple(sorted({str(item[4][0]) for item in found}))


class Pool(Protocol):
    def submit(self, fn: Callable[[str], tuple[str, ...]], name: str, /) -> Future[Any]: ...


class Domains:
    """The own domains of the split groups and their addresses.

    The names are looked up in the background, all at once every `interval` seconds and right
    after a group gets a new list, so a slow resolver never holds up the probes. A name that
    does not resolve keeps the addresses it had. Nothing about the names is logged: they are
    resources the users visit.
    """

    def __init__(
        self,
        clock: Callable[[], float],
        lookup: Callable[[str], tuple[str, ...]] = resolve,
        pool: Pool | None = None,
        interval: float = RESOLVE_INTERVAL,
    ) -> None:
        self._clock = clock
        self._lookup = lookup
        self._pool = pool if pool is not None else ThreadPoolExecutor(RESOLVE_WORKERS)
        self._interval = interval
        self._lists: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        self._addresses: dict[str, tuple[str, ...]] = {}
        self._round: dict[str, Future[Any]] = {}
        self._due = 0.0

    def set(self, port: int, local: tuple[str, ...], abroad: tuple[str, ...]) -> bool:
        """Give the group its domains; True when its addresses change."""
        before = self.addresses(port)
        self._lists[port] = (local, abroad)
        if not set(local + abroad) <= self._addresses.keys():
            self._due = 0.0
        return self.addresses(port) != before

    def drop(self, port: int) -> None:
        self._lists.pop(port, None)

    def poll(self) -> bool:
        """Take a finished round and start one that is due; True when addresses changed."""
        changed = False
        if self._round and all(future.done() for future in self._round.values()):
            changed = self._take()
        if not self._round and self._lists and self._clock() >= self._due:
            names = sorted(
                {name for pair in self._lists.values() for part in pair for name in part}
            )
            self._round = {name: self._pool.submit(self._lookup, name) for name in names}
            self._due = self._clock() + self._interval
            if all(future.done() for future in self._round.values()):
                changed = self._take() or changed
        return changed

    def addresses(self, port: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The addresses of the group's local and abroad domains."""
        local, abroad = self._lists.get(port, ((), ()))
        return self._of(local), self._of(abroad)

    def _of(self, names: Iterable[str]) -> tuple[str, ...]:
        return tuple(
            sorted({address for name in names for address in self._addresses.get(name, ())})
        )

    def _take(self) -> bool:
        before = {port: self.addresses(port) for port in self._lists}
        for name, future in self._round.items():
            found = None if future.exception() is not None else future.result()
            if found:
                self._addresses[name] = found
        self._round = {}
        wanted = {name for pair in self._lists.values() for part in pair for name in part}
        for name in set(self._addresses) - wanted:
            del self._addresses[name]
        return any(self.addresses(port) != addresses for port, addresses in before.items())


def resolver_config(subnets: Iterable[str]) -> str:
    """The unbound config: answers the clients of these subnets only, asks the upstreams over
    DNS-over-TLS and logs nothing of the queries and answers, not even to the standard output.
    """
    allowed = [f"\taccess-control: {subnet} allow" for subnet in sorted(set(subnets))]
    upstreams = [f"\tforward-addr: {upstream}" for upstream in RESOLVER_UPSTREAMS]
    lines = [
        "server:",
        "\tinterface-automatic: yes",
        f"\tport: {DNS_PORT}",
        "\tdo-ip6: no",
        '\tusername: "unbound"',
        '\tchroot: ""',
        "\tdo-daemonize: no",
        "\tuse-syslog: no",
        '\tlogfile: "/dev/null"',
        "\tverbosity: 0",
        "\tlog-queries: no",
        "\tlog-replies: no",
        "\tlog-servfail: no",
        "\tlog-local-actions: no",
        "\tval-log-level: 0",
        "\tstatistics-interval: 0",
        "\thide-identity: yes",
        "\thide-version: yes",
        '\tmodule-config: "iterator"',
        f'\ttls-cert-bundle: "{RESOLVER_CERTIFICATES}"',
        "\taccess-control: 0.0.0.0/0 refuse",
        *allowed,
        "forward-zone:",
        '\tname: "."',
        "\tforward-tls-upstream: yes",
        *upstreams,
    ]
    return "\n".join(lines) + "\n"


class Process(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


def start_unbound(config: Path) -> Process:
    """unbound in the foreground with no pid file and its output dropped; OSError if it is not
    installed or the config is bad."""
    check = subprocess.run(
        ["unbound-checkconf", str(config)], capture_output=True, text=True, check=False
    )
    if check.returncode != 0:
        raise OSError(f"bad config: {(check.stdout + check.stderr).strip()}")
    return subprocess.Popen(
        ["unbound", "-d", "-p", "-c", str(config)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip


class Resolver:
    """The DNS resolver of the split groups' clients, running while some group is split.

    A resolver that did not start or stopped is started again after `RESOLVER_RETRY` seconds;
    until then the queries are not redirected and go where the keys send them. An old relay
    image has no unbound: the clients' DNS stays as it was.
    """

    def __init__(
        self,
        folder: Path,
        clock: Callable[[], float],
        log: Callable[[str], None],
        start: Callable[[Path], Process] = start_unbound,
    ) -> None:
        self._config = folder / RESOLVER_CONFIG
        self._clock = clock
        self._log = log
        self._start = start
        self._process: Process | None = None
        self._text: str | None = None
        self._wanted = False
        self._retry_at = 0.0
        self._failure: str | None = None

    def serve(self, subnets: Sequence[str]) -> bool:
        """Answer the clients of these subnets, or stop for none; True while it runs."""
        self._wanted = bool(subnets)
        if not subnets:
            self.stop()
            return False
        text = resolver_config(subnets)
        if self._process is not None and text == self._text:
            return True
        self.stop()
        if self._clock() < self._retry_at:
            return False
        _write_secret(self._config, text)
        try:
            self._process = self._start(self._config)
        except OSError as error:
            self._fail(f"resolver not started: {error}")
            return False
        self._text, self._failure = text, None
        return True

    def stale(self) -> bool:
        """True when it stopped by itself or is due to start again: the rules change."""
        if self._process is not None and (code := self._process.poll()) is not None:
            self._process = self._text = None
            self._fail(f"resolver stopped with code {code}")
            return True
        return self._wanted and self._process is None and self._clock() >= self._retry_at

    def stop(self) -> None:
        process, self._process, self._text = self._process, None, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(RESOLVER_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _fail(self, message: str) -> None:
        self._retry_at = self._clock() + RESOLVER_RETRY
        if message != self._failure:
            self._log(message)
        self._failure = message


class Agent:
    """One pass of `tick` reads a changed config, probes every group and writes the state.

    A config the agent cannot read keeps the previous one: the relay goes on forwarding. The
    peers, the network list and the own domains of a split group are files of their own, taken
    when they change; an empty or broken list keeps the one the group has, a missing one is
    empty: the panel removed it.
    """

    def __init__(  # noqa: PLR0913  the domains and the resolver are replaced in tests
        self,
        network: Network,
        folder: Path = FOLDER,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = print,
        *,
        domains: Domains | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._network = network
        self._folder = folder
        self._config = folder / "config.json"
        self._state = folder / "state.json"
        self._request = folder / "switch.json"
        self._clock = clock
        self._log = log
        self._loaded: float | None = None
        self.settings = Settings((), 5, 3)
        self._watches: dict[int, Watch] = {}
        self._lanes: dict[int, Lane] = {}
        self._networks: dict[int, tuple[str, ...]] = {}
        self._domains = domains if domains is not None else Domains(clock)
        self._resolver = resolver if resolver is not None else Resolver(folder, clock, log)
        # Modification times of the split groups' files as last taken.
        self._seen: dict[str, float | None] = {}
        # Ports whose routing changed: their clients' conntrack entries go with the new rules.
        self._rerouted: set[int] = set()
        self._traffic = Traffic()
        self._dirty = True
        self._remembered, self._modes = self._previous_choice()
        self._answer: dict[str, str] | None = None

    def tick(self) -> None:
        self._reload()
        self._take_split_files()
        if self._domains.poll() or self._resolver.stale():
            self._dirty = True
        now = int(self._clock())
        moved = set(self._rerouted)
        requested = self._take_request()
        asked = self._switch(*requested, now) if requested is not None else None
        for port, watch in self._watches.items():
            before = watch.active
            switched = watch.check(functools.partial(self._network.probe, watch.group), now)
            if switched:
                self._log(f"group {port}: {before or 'none'} -> {watch.active}")
            switched = switched or port == asked
            lane = self._lanes.get(port)
            if lane is not None:
                switched = self._steer(watch, lane, switched, now)
            if switched:
                moved.add(port)
            else:
                watch.observe(self._network.flows(port))
        if moved or self._dirty:
            self._traffic.keep(self._network.counters())
            self._network.forward(self._rules())
            for port in sorted(moved):
                self._network.forget(port)
            self._dirty = False
            self._rerouted.clear()
        self._write_state(now)

    def run(self) -> None:
        while True:
            started = self._clock()
            try:
                self.tick()
            except (OSError, subprocess.SubprocessError) as error:
                self._log(f"tick failed: {error}")
                self._dirty = True
            time.sleep(max(0.0, self.settings.check_interval - (self._clock() - started)))

    def _reload(self) -> None:
        try:
            changed = self._config.stat().st_mtime
        except FileNotFoundError:
            return
        if changed == self._loaded:
            return
        self._loaded = changed
        try:
            settings = Settings.parse(self._config.read_text(encoding="utf-8"))
        except ValueError as error:
            self._log(f"config ignored: {error}")
            return
        self._apply(settings)

    def _apply(self, settings: Settings) -> None:
        ports = {group.port for group in settings.groups}
        for port in set(self._watches) - ports:
            self._network.drop(port)
            del self._watches[port]
            self._lanes.pop(port, None)
            self._networks.pop(port, None)
            self._domains.drop(port)
        for group in settings.groups:
            self._network.prepare(group)
            watch = self._watches.get(group.port)
            if watch is None:
                active = self._remembered.get(group.port)
                self._watches[group.port] = Watch(group, settings.failures, active)
            else:
                watch.update(group, settings.failures)
            self._route(group, settings)
        self.settings = settings
        self._dirty = True

    def _route(self, group: Group, settings: Settings) -> None:
        """Set the group up for the routing its config asks; the clone is (re)applied next."""
        port, lane = group.port, self._lanes.get(group.port)
        if group.split is None:
            if lane is not None:
                self._network.unclone(port)
                del self._lanes[port]
                self._networks.pop(port, None)
                self._domains.drop(port)
                self._rerouted.add(port)
            return
        if lane is None:
            mode = self._modes.pop(port, SPLIT)
            self._lanes[port] = Lane(settings.failures, settings.recovery, mode)
            self._rerouted.add(port)
            self._seen.pop(_networks_file(port), None)
            self._seen.pop(_domains_file(port), None)
        else:
            lane.update(settings.failures, settings.recovery)
        self._seen.pop(_peers_file(port), None)

    def _take_split_files(self) -> None:
        """Apply a changed peers file, network list or domain list of each split group."""
        for port, lane in self._lanes.items():
            group = self._watches[port].group
            cloned = False
            if self._changed(_peers_file(port)):
                try:
                    self._network.clone(group)
                    cloned = True
                    if lane.endpoint is not None:
                        self._network.point(group, lane.endpoint)
                except (OSError, subprocess.SubprocessError) as error:
                    self._log(f"group {port}: clone not applied: {error}")
            # A clone created anew has no tc rules: the limits go on again after every clone.
            if self._changed(_speed_file(port)) or cloned:
                self._take_speed(port)
            if self._changed(_networks_file(port)):
                self._take_networks(port)
            if self._changed(_domains_file(port)):
                self._take_domains(port)

    def _changed(self, name: str) -> bool:
        try:
            moment: float | None = (self._folder / name).stat().st_mtime
        except FileNotFoundError:
            moment = None
        if name in self._seen and self._seen[name] == moment:
            return False
        self._seen[name] = moment
        return True

    def _take_speed(self, port: int) -> None:
        script = self._folder / _speed_file(port)
        if not script.exists():
            return
        try:
            self._network.limit(script)
        except (OSError, subprocess.SubprocessError) as error:
            self._log(f"group {port}: speed limits not applied: {error}")

    def _take_networks(self, port: int) -> None:
        try:
            text = (self._folder / _networks_file(port)).read_text(encoding="utf-8")
            networks = parse_networks(text)
        except FileNotFoundError:
            networks = ()
        except ValueError as error:
            self._log(f"group {port}: network list ignored: {error}")
            return
        if networks != self._networks.get(port):
            self._networks[port] = networks
            self._dirty = True

    def _take_domains(self, port: int) -> None:
        try:
            text = (self._folder / _domains_file(port)).read_text(encoding="utf-8")
            local, abroad = parse_domains(text)
        except FileNotFoundError:
            local, abroad = (), ()
        except ValueError as error:
            self._log(f"group {port}: domain list ignored: {error}")
            return
        if self._domains.set(port, local, abroad):
            self._dirty = True

    def _steer(self, watch: Watch, lane: Lane, switched: bool, now: int) -> bool:
        """One round of a split group: keep the uplink on its member and count its check.

        True when the group's rules change: its mode changed, or in the forward fallback the
        group moved to another member.
        """
        group = watch.group
        reroute = switched and not lane.split
        if watch.active is not None and (
            lane.endpoint is None or (lane.split and lane.endpoint != watch.active)
        ):
            lane.point(watch.active)
            self._network.point(group, watch.active)
            return reroute  # the new handshake is under way: the next round checks it
        if lane.endpoint is None:
            return reroute
        alive = self._network.uplink_alive(group, self.settings.uplink_check)
        change = lane.record(alive, watch, now)
        if change == Lane.POINT and lane.endpoint is not None:
            self._log(f"group {group.port}: uplink -> {lane.endpoint}")
            self._network.point(group, lane.endpoint)
        elif change == Lane.REROUTE:
            self._log(f"group {group.port}: {lane.mode} mode, member {watch.active}")
            return True
        return reroute

    def _rules(self) -> str:
        """The rules of every group; the resolver runs before its queries are redirected."""
        routes: dict[int, Backend | None] = {}
        split: dict[int, Split] = {}
        for port, watch in self._watches.items():
            lane = self._lanes.get(port)
            if lane is not None and lane.split and watch.group.split is not None:
                split[port] = watch.group.split
            else:
                routes[port] = watch.active
        serving = self._resolver.serve([group.subnet for group in split.values()])
        splits = [self._split_route(port, group, serving) for port, group in split.items()]
        return ruleset(routes, splits)

    def _split_route(self, port: int, split: Split, serving: bool) -> SplitRoute:
        local, abroad = self._domains.addresses(port)
        dns = split.dns if serving else ()
        networks = self._networks.get(port, ())
        return SplitRoute(port, split.subnet, networks, local, abroad, dns, split.list_mode)

    def _take_request(self) -> tuple[str, int, Backend] | None:
        """The switch request of the panel, removed so it is done once; a bad one is dropped."""
        try:
            text = self._request.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        self._request.unlink(missing_ok=True)
        try:
            data = json.loads(text)
            if data.get("version") != FORMAT_VERSION:
                raise ValueError(f"unknown request version {data.get('version')!r}")
            return str(data["id"]), int(data["port"]), Backend.from_dict(data["backend"])
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            self._log(f"switch request ignored: {error!r}")
            return None

    def _switch(self, request: str, port: int, backend: Backend, now: int) -> int | None:
        """Do the switch the panel asked for; the moved port, if any."""
        watch = self._watches.get(port)
        before = watch.active if watch else None
        if watch is None:
            outcome = UNKNOWN_GROUP
        else:
            outcome = watch.switch_to(
                backend, functools.partial(self._network.probe, watch.group), now
            )
        self._log(f"group {port}: switch to {backend} asked: {outcome}")
        self._answer = {"id": request, "outcome": outcome}
        if outcome != SWITCHED:
            return None
        self._log(f"group {port}: {before or 'none'} -> {backend}")
        return port

    def _previous_choice(self) -> tuple[dict[int, Backend], dict[int, str]]:
        """The active members and split modes of the last run: a restarted agent stays on them."""
        try:
            groups = json.loads(self._state.read_text(encoding="utf-8"))["groups"]
            active = {
                int(group["port"]): Backend.from_dict(group["active"])
                for group in groups
                if group.get("active")
            }
            modes = {
                int(group["port"]): group["mode"]
                for group in groups
                if group.get("routing") == SPLIT and group.get("mode") in (SPLIT, FORWARD)
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}, {}
        return active, modes

    def _write_state(self, now: int) -> None:
        current = self._network.counters()
        groups = [
            {**watch.state(self._traffic.total(port, current)), **self._split_state(port)}
            for port, watch in sorted(self._watches.items())
        ]
        data: dict[str, Any] = {"version": FORMAT_VERSION, "updated_at": now, "groups": groups}
        if self._answer is not None:
            data["switch"] = self._answer
        text = json.dumps(data)
        temporary = self._state.with_suffix(".tmp")
        temporary.write_text(text + "\n", encoding="utf-8")
        temporary.replace(self._state)

    def _split_state(self, port: int) -> dict[str, Any]:
        lane = self._lanes.get(port)
        if lane is None:
            return {"routing": FORWARD, "mode": FORWARD}
        return {
            "routing": SPLIT,
            "mode": lane.mode,
            "mode_changed_at": lane.changed_at,
            "uplink": lane.state(),
            "networks": len(self._networks.get(port, ())),
        }


def _peers_file(port: int) -> str:
    return f"peers-{port}.conf"


def _speed_file(port: int) -> str:
    return f"speed-{port}.sh"


def _networks_file(port: int) -> str:
    return f"networks-{port}.txt"


def _domains_file(port: int) -> str:
    return f"domains-{port}.txt"


class AwgUplink:
    """The uplink as an AmneziaWG client of the group, with the group's parameters: filters on
    the way pass it like any client (docs/SPLIT.md section 10). Another kind of uplink, such as
    XRay Reality, would be a class with the same methods."""

    def __init__(self, folder: Path) -> None:
        self._folder = folder

    def open(self, group: Group) -> None:
        split, name = _split_of(group), group.uplink_interface
        created = not _ok("ip", "link", "show", name)
        if created:
            _run("amneziawg-go", name)
        uplink = split.uplink
        lines = ["[Interface]", f"PrivateKey = {uplink['private_key']}"]
        lines += [f"{key} = {value}" for key, value in uplink["awg_params"].items()]
        changed = _write_secret(self._folder / f"{name}.conf", "\n".join(lines) + "\n")
        _write_secret(self._folder / f"{name}.psk", f"{uplink['preshared_key']}\n")
        if created or changed:
            # setconf drops the peer: the agent points the uplink again right after.
            _run("awg", "setconf", name, str(self._folder / f"{name}.conf"))
        _run("ip", "address", "replace", str(uplink["address"]), "dev", name)
        _run("ip", "link", "set", name, "up")

    def point(self, group: Group, backend: Backend) -> None:
        name, key = group.uplink_interface, str(_split_of(group).uplink["server_public_key"])
        _run(
            "awg", "set", name, "peer", key, "preshared-key", str(self._folder / f"{name}.psk"),
            "endpoint", str(backend), "allowed-ips", "0.0.0.0/0", "persistent-keepalive", "25",
        )  # fmt: skip

    @staticmethod
    def alive(group: Group, target: str) -> bool:
        interface = group.uplink_interface
        return _ok("ping", "-c", "1", "-W", str(UPLINK_TIMEOUT), "-I", interface, target)

    @staticmethod
    def close(port: int) -> None:
        _ok("ip", "link", "del", f"lu{port}")


class Shell:
    """The real container network: amneziawg-go, awg, ip, nft, conntrack and ping."""

    def __init__(self, folder: Path = FOLDER, timeout: float = PROBE_TIMEOUT) -> None:
        self._folder = folder
        self._timeout = timeout
        self._uplinks = {"awg": AwgUplink(folder)}

    def prepare(self, group: Group) -> None:
        name = group.interface
        if not _ok("ip", "link", "show", name):
            _run("amneziawg-go", name)
        service = group.service
        lines = ["[Interface]", f"PrivateKey = {service['private_key']}"]
        lines += [f"{key} = {value}" for key, value in service["awg_params"].items()]
        _write_secret(self._folder / f"{name}.conf", "\n".join(lines) + "\n")
        _write_secret(self._folder / f"{name}.psk", f"{service['preshared_key']}\n")
        _run("awg", "setconf", name, str(self._folder / f"{name}.conf"))
        _run("ip", "link", "set", name, "up")

    def drop(self, port: int) -> None:
        _ok("ip", "link", "del", f"lr{port}")
        self.unclone(port)

    def probe(self, group: Group, backend: Backend) -> bool:
        name, key = group.interface, str(group.service["server_public_key"])
        _ok("awg", "set", name, "peer", key, "remove")
        psk = str(self._folder / f"{name}.psk")
        # Listen port 0 binds a new random port, so the handshake is a new flow.
        _run(
            "awg", "set", name, "listen-port", "0", "peer", key, "preshared-key", psk,
            "endpoint", str(backend), "persistent-keepalive", "25",
        )  # fmt: skip
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if _handshaken(_run("awg", "show", name, "latest-handshakes")):
                return True
            time.sleep(_PROBE_POLL)
        return False

    def forward(self, rules: str) -> None:
        subprocess.run(["nft", "-f", "-"], input=rules, text=True, check=True)

    def forget(self, port: int) -> None:
        # conntrack exits 1 when there was nothing to delete.
        _ok("conntrack", "-D", "-p", "udp", "--orig-port-dst", str(port))

    def flows(self, port: int) -> list[Flow]:
        found = subprocess.run(
            ["conntrack", "-L", "-p", "udp", "--orig-port-dst", str(port)],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        return parse_flows(found.stdout)

    def counters(self) -> dict[int, tuple[int, int]]:
        found = subprocess.run(
            ["nft", "-j", "list", "counters", "table", "ip", TABLE],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        return parse_counters(found.stdout) if found.returncode == 0 else {}

    def clone(self, group: Group) -> None:
        split, name = _split_of(group), group.clone_interface
        if not _ok("ip", "link", "show", name):
            _run("amneziawg-go", name)
        lines = ["[Interface]", f"PrivateKey = {split.clone['private_key']}"]
        lines += [f"ListenPort = {group.port}"]
        lines += [f"{key} = {value}" for key, value in split.clone["awg_params"].items()]
        try:
            peers = (self._folder / _peers_file(group.port)).read_text(encoding="utf-8")
        except FileNotFoundError:
            peers = ""
        _write_secret(self._folder / f"{name}.conf", "\n".join(lines) + "\n\n" + peers)
        # syncconf keeps the sessions of the peers that stay.
        _run("awg", "syncconf", name, str(self._folder / f"{name}.conf"))
        _run("ip", "address", "replace", str(split.clone["address"]), "dev", name)
        _run("ip", "link", "set", name, "up")
        self._uplinks[split.kind].open(group)
        mark = str(group.mark)
        _run("ip", "route", "replace", "default", "dev", group.uplink_interface, "table", mark)
        if f"fwmark {hex(group.mark)} lookup {mark}" not in _run("ip", "rule", "show"):
            _run("ip", "rule", "add", "fwmark", mark, "lookup", mark)

    def unclone(self, port: int) -> None:
        _ok("ip", "link", "del", f"lg{port}")
        _ok("ip", "link", "del", f"ifb_lg{port}")
        for uplink in self._uplinks.values():
            uplink.close(port)
        mark = str(MARK_BASE + port)
        _ok("ip", "rule", "del", "fwmark", mark, "lookup", mark)

    def limit(self, script: Path) -> None:
        _run("sh", str(script))

    def point(self, group: Group, backend: Backend) -> None:
        self._uplinks[_split_of(group).kind].point(group, backend)

    def uplink_alive(self, group: Group, target: str) -> bool:
        return self._uplinks[_split_of(group).kind].alive(group, target)


def _split_of(group: Group) -> Split:
    if group.split is None:
        raise ValueError(f"group {group.port} is not split")
    return group.split


def _write_secret(path: Path, text: str) -> bool:
    """Write a file readable by root only; True if its content changed."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except FileNotFoundError:
        pass
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(text)
    return True


def _run(*command: str) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout


def _ok(*command: str) -> bool:
    return subprocess.run(command, capture_output=True, check=False).returncode == 0


def _handshaken(output: str) -> bool:
    """`awg show <if> latest-handshakes` has a peer with a handshake time other than 0."""
    return any(line.split()[-1:] not in ([], ["0"]) for line in output.splitlines())


def parse_flows(text: str) -> list[Flow]:
    """Client entries from `conntrack -L` text; the probes of the agent are left out.

    A client entry went through DNAT: its answer comes from another address than the one it was
    sent to. A probe goes from the relay straight to the member.
    """
    flows = []
    for line in text.splitlines():
        pairs = [token.split("=", 1) for token in line.split() if "=" in token]
        sources = [value for key, value in pairs if key == "src"]
        targets = [value for key, value in pairs if key == "dst"]
        if len(sources) != len(targets) or len(sources) < DIRECTIONS:
            continue
        (client, member), (sent_to, _) = sources[:2], targets[:2]
        if sent_to != member:
            flows.append(Flow(client, member, "[UNREPLIED]" not in line))
    return flows


def parse_counters(text: str) -> dict[int, tuple[int, int]]:
    """Bytes by port from `nft -j list counters` of the relay table."""
    found: dict[int, list[int]] = {}
    for item in _counters(json.loads(text or "{}").get("nftables", ())):
        direction, _, port = str(item.get("name", "")).rpartition("_")
        if not port.isdigit() or direction not in ("from_clients", "to_clients"):
            continue
        pair = found.setdefault(int(port), [0, 0])
        pair[direction == "to_clients"] = int(item.get("bytes", 0))
    return {port: (pair[0], pair[1]) for port, pair in found.items()}


def _counters(items: Iterable[Any]) -> Sequence[Mapping[str, Any]]:
    return [item["counter"] for item in items if isinstance(item, dict) and "counter" in item]


if __name__ == "__main__":  # pragma: no cover - runs in the relay container
    agent = Agent(Shell(), log=lambda line: print(line, file=sys.stderr, flush=True))
    agent.run()
