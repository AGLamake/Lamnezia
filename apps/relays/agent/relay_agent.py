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
listens on its port, packets to the networks of its list and to private networks leave the
relay directly, the rest gets a firewall mark and goes over the uplink `lu<port>`, a client of
the group on the active member. A new active member only changes the uplink's endpoint, so the
clients' sessions stay. When the uplink fails on every live member, the group falls back to
forwarding (`Lane`).
"""

import functools
import ipaddress
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
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
# Firewall mark and route table of a split group: the base plus the port, clear of the main ones.
MARK_BASE = 0x10000
# Never sent over the uplink: private, loopback, link-local and multicast networks.
LOCAL_NETWORKS = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/3",
)  # fmt: skip

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

    @property
    def subnet(self) -> str:
        return str(ipaddress.IPv4Interface(self.clone["address"]).network)

    @classmethod
    def from_dict(cls, data: Any) -> "Split":
        if not isinstance(data, dict):
            raise ValueError("split must be an object")
        split = cls(dict(data["clone"]), dict(data["uplink"]), str(data.get("kind", "awg")))
        if split.kind not in UPLINK_KINDS:
            raise ValueError(f"unknown uplink kind {split.kind!r}")
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
    failed checks in a row it moves to the next member that answers probes and has not failed
    the uplink yet. When none is left but a member still answers a probe, the group falls back
    to forwarding its port to that member; when no member answers at all it stays split, so
    Russian sites still work. In the forward fallback the uplink is checked on: after a failure
    it tries the next live member, and `recovery` good checks in a row split the group again.
    """

    POINT = "point"
    REROUTE = "reroute"

    def __init__(self, limit: int, recovery: int, mode: str = SPLIT) -> None:
        self.limit, self.recovery, self.mode = limit, recovery, mode
        self.changed_at: int | None = None
        self.endpoint: Backend | None = None
        self.health = Health()
        self.successes = 0
        # Members whose uplink failed since the last good check.
        self.failed: set[Backend] = set()

    @property
    def split(self) -> bool:
        return self.mode == SPLIT

    def update(self, limit: int, recovery: int) -> None:
        self.limit, self.recovery = limit, recovery

    def point(self, backend: Backend) -> None:
        self.endpoint = backend
        self.health.failures = self.successes = 0

    def record(self, alive: bool, watch: Watch, now: int) -> str | None:
        """Count one check of the uplink: POINT when it must go to `endpoint`, REROUTE when the
        mode changed and with it the group's rules."""
        self.health.record(alive, now)
        if alive:
            return self._succeed(watch, now)
        self.successes = 0
        if not self.split:
            return self._try_next(watch)
        if self.health.failures < self.limit:
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
        self.failed.clear()
        self.successes += 1
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
        self.health.failures = self.successes = 0


@dataclass(frozen=True)
class SplitRoute:
    """The nftables part of a split group: its clone's subnet and the networks sent directly."""

    port: int
    subnet: str
    networks: tuple[str, ...] = ()


def ruleset(routes: Mapping[int, Backend | None], splits: Sequence[SplitRoute] = ()) -> str:
    """The whole nftables table for one `nft -f`: the active member of each forwarded port and
    the marks of each split group.

    Declaring and deleting the table first replaces it atomically, with no moment without rules,
    and a new network list with it. Masquerade keeps the relay in the path: without it the
    member answers the client directly. A packet from a split group's clone to an address out of
    its list and the private networks gets the group's mark, which routes it into the uplink.
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
        sets += _set(f"ru_{port}", split.networks)
        marks.append(
            f'\t\tiifname "{clone}" ip daddr != @local ip daddr != @ru_{port} '
            f"meta mark set {MARK_BASE + port}"
        )
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


class Agent:
    """One pass of `tick` reads a changed config, probes every group and writes the state.

    A config the agent cannot read keeps the previous one: the relay goes on forwarding. The
    peers and the network list of a split group are files of their own, taken when they change;
    an empty or broken list keeps the one the group has.
    """

    def __init__(
        self,
        network: Network,
        folder: Path = FOLDER,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = print,
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
                self._rerouted.add(port)
            return
        if lane is None:
            mode = self._modes.pop(port, SPLIT)
            self._lanes[port] = Lane(settings.failures, settings.recovery, mode)
            self._rerouted.add(port)
            self._seen.pop(_networks_file(port), None)
        else:
            lane.update(settings.failures, settings.recovery)
        self._seen.pop(_peers_file(port), None)

    def _take_split_files(self) -> None:
        """Apply a changed peers file or network list of each split group."""
        for port, lane in self._lanes.items():
            group = self._watches[port].group
            if self._changed(_peers_file(port)):
                try:
                    self._network.clone(group)
                    if lane.endpoint is not None:
                        self._network.point(group, lane.endpoint)
                except (OSError, subprocess.SubprocessError) as error:
                    self._log(f"group {port}: clone not applied: {error}")
            if self._changed(_networks_file(port)):
                self._take_networks(port)

    def _changed(self, name: str) -> bool:
        try:
            moment: float | None = (self._folder / name).stat().st_mtime
        except FileNotFoundError:
            moment = None
        if name in self._seen and self._seen[name] == moment:
            return False
        self._seen[name] = moment
        return True

    def _take_networks(self, port: int) -> None:
        try:
            text = (self._folder / _networks_file(port)).read_text(encoding="utf-8")
            networks = parse_networks(text)
        except FileNotFoundError:
            return
        except ValueError as error:
            self._log(f"group {port}: network list ignored: {error}")
            return
        if networks != self._networks.get(port):
            self._networks[port] = networks
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
        routes: dict[int, Backend | None] = {}
        splits: list[SplitRoute] = []
        for port, watch in self._watches.items():
            lane, split = self._lanes.get(port), watch.group.split
            if lane is not None and lane.split and split is not None:
                splits.append(SplitRoute(port, split.subnet, self._networks.get(port, ())))
            else:
                routes[port] = watch.active
        return ruleset(routes, splits)

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


def _networks_file(port: int) -> str:
    return f"networks-{port}.txt"


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
        for uplink in self._uplinks.values():
            uplink.close(port)
        mark = str(MARK_BASE + port)
        _ok("ip", "rule", "del", "fwmark", mark, "lookup", mark)

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
