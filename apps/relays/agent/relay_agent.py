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
"""

import functools
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
# Unanswered clients needed to call the active member silent: one is likely a deleted key.
SILENT_CLIENTS = 2
DIRECTIONS = 2  # a conntrack entry lists the original and the reply direction

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
class Group:
    """A group from the config: its port, the members by priority and the service peer."""

    port: int
    backends: tuple[Backend, ...]
    service: Mapping[str, Any]

    @property
    def interface(self) -> str:
        return f"lr{self.port}"


@dataclass(frozen=True)
class Settings:
    groups: tuple[Group, ...]
    check_interval: int
    failures: int

    @classmethod
    def parse(cls, text: str) -> "Settings":
        """The config the panel wrote; ValueError if it is not one this agent knows."""
        try:
            data = json.loads(text)
            if data.get("version") != FORMAT_VERSION:
                raise ValueError(f"unknown config version {data.get('version')!r}")
            groups = tuple(
                Group(
                    int(item["port"]),
                    tuple(Backend.from_dict(backend) for backend in item["backends"]),
                    dict(item["service"]),
                )
                for item in data["groups"]
            )
            return cls(groups, int(data["check_interval"]), int(data["failures"]))
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError(f"bad config: {error!r}") from error


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

    def check(self, probe: Callable[[Backend], bool], now: int) -> bool:
        """Probe every member; True if the group moved to another one."""
        for backend in self.group.backends:
            self.health[backend].record(probe(backend), now)
        if self.active in self.health and self.health[self.active].failures < self.limit:
            return False
        for candidate in self.group.backends:
            if candidate != self.active and self.health[candidate].alive:
                self._move(candidate, now)
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
        self._move(backend, now)
        return SWITCHED

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

    def _move(self, backend: Backend, now: int) -> None:
        self.active, self.switched_at, self.silent = backend, now, 0


def ruleset(routes: Mapping[int, Backend | None]) -> str:
    """The whole nftables table for the active member of each group port, for one `nft -f`.

    Declaring and deleting the table first replaces it atomically, with no moment without rules.
    Masquerade keeps the relay in the path: without it the member answers the client directly.
    """
    counters: list[str] = []
    dnat: list[str] = []
    masquerade: list[str] = []
    count: list[str] = []
    for port, backend in sorted(routes.items()):
        counters += [f"\tcounter from_clients_{port} {{ }}", f"\tcounter to_clients_{port} {{ }}"]
        if backend is None:
            continue
        member = f"ip daddr {backend.host} udp dport {backend.port}"
        dnat.append(f"\t\tudp dport {port} dnat to {backend}")
        masquerade.append(f"\t\t{member} masquerade")
        count.append(f'\t\t{member} counter name "from_clients_{port}"')
        count.append(
            f'\t\tip saddr {backend.host} udp sport {backend.port} counter name "to_clients_{port}"'
        )
    lines = [
        f"table ip {TABLE}",
        f"delete table ip {TABLE}",
        f"table ip {TABLE} {{",
        *counters,
        "\tchain prerouting {",
        "\t\ttype nat hook prerouting priority dstnat; policy accept;",
        *dnat,
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


class Network(Protocol):
    """What the agent does to the container network; `Shell` runs the real commands."""

    def prepare(self, group: Group) -> None:
        """Bring up the service interface of the group with its service peer keys."""

    def drop(self, port: int) -> None:
        """Remove the service interface of a group that left the config."""

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

    A config the agent cannot read keeps the previous one: the relay goes on forwarding.
    """

    def __init__(
        self,
        network: Network,
        folder: Path = FOLDER,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = print,
    ) -> None:
        self._network = network
        self._config = folder / "config.json"
        self._state = folder / "state.json"
        self._request = folder / "switch.json"
        self._clock = clock
        self._log = log
        self._loaded: float | None = None
        self.settings = Settings((), 5, 3)
        self._watches: dict[int, Watch] = {}
        self._traffic = Traffic()
        self._dirty = True
        self._remembered = self._previous_choice()
        self._answer: dict[str, str] | None = None

    def tick(self) -> None:
        self._reload()
        now = int(self._clock())
        moved: list[int] = []
        requested = self._take_request()
        if requested is not None:
            moved += self._switch(*requested, now)
        for port, watch in self._watches.items():
            before = watch.active
            if watch.check(functools.partial(self._network.probe, watch.group), now):
                self._log(f"group {port}: {before or 'none'} -> {watch.active}")
                if port not in moved:
                    moved.append(port)
            elif port not in moved:
                watch.observe(self._network.flows(port))
        if moved or self._dirty:
            self._traffic.keep(self._network.counters())
            self._network.forward(ruleset({p: w.active for p, w in self._watches.items()}))
            for port in moved:
                self._network.forget(port)
            self._dirty = False
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
        for group in settings.groups:
            self._network.prepare(group)
            watch = self._watches.get(group.port)
            if watch is None:
                active = self._remembered.get(group.port)
                self._watches[group.port] = Watch(group, settings.failures, active)
            else:
                watch.update(group, settings.failures)
        self.settings = settings
        self._dirty = True

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

    def _switch(self, request: str, port: int, backend: Backend, now: int) -> list[int]:
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
            return []
        self._log(f"group {port}: {before or 'none'} -> {backend}")
        return [port]

    def _previous_choice(self) -> dict[int, Backend]:
        """The active members of the last run: a restarted agent stays on them while they live."""
        try:
            data = json.loads(self._state.read_text(encoding="utf-8"))
            return {
                int(group["port"]): Backend.from_dict(group["active"])
                for group in data["groups"]
                if group.get("active")
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def _write_state(self, now: int) -> None:
        current = self._network.counters()
        groups = [
            watch.state(self._traffic.total(port, current))
            for port, watch in sorted(self._watches.items())
        ]
        data = {"version": FORMAT_VERSION, "updated_at": now, "groups": groups}
        if self._answer is not None:
            data["switch"] = self._answer
        text = json.dumps(data)
        temporary = self._state.with_suffix(".tmp")
        temporary.write_text(text + "\n", encoding="utf-8")
        temporary.replace(self._state)


class Shell:
    """The real container network: amneziawg-go, awg, ip, nft and conntrack."""

    def __init__(self, folder: Path = FOLDER, timeout: float = PROBE_TIMEOUT) -> None:
        self._folder = folder
        self._timeout = timeout

    def prepare(self, group: Group) -> None:
        name = group.interface
        if not self._ok("ip", "link", "show", name):
            self._run("amneziawg-go", name)
        service = group.service
        lines = ["[Interface]", f"PrivateKey = {service['private_key']}"]
        lines += [f"{key} = {value}" for key, value in service["awg_params"].items()]
        self._secret(f"{name}.conf", "\n".join(lines) + "\n")
        self._secret(f"{name}.psk", f"{service['preshared_key']}\n")
        self._run("awg", "setconf", name, str(self._folder / f"{name}.conf"))
        self._run("ip", "link", "set", name, "up")

    def drop(self, port: int) -> None:
        self._ok("ip", "link", "del", f"lr{port}")

    def probe(self, group: Group, backend: Backend) -> bool:
        name, key = group.interface, str(group.service["server_public_key"])
        self._ok("awg", "set", name, "peer", key, "remove")
        psk = str(self._folder / f"{name}.psk")
        # Listen port 0 binds a new random port, so the handshake is a new flow.
        self._run(
            "awg", "set", name, "listen-port", "0", "peer", key, "preshared-key", psk,
            "endpoint", str(backend), "persistent-keepalive", "25",
        )  # fmt: skip
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if _handshaken(self._run("awg", "show", name, "latest-handshakes")):
                return True
            time.sleep(_PROBE_POLL)
        return False

    def forward(self, rules: str) -> None:
        subprocess.run(["nft", "-f", "-"], input=rules, text=True, check=True)

    def forget(self, port: int) -> None:
        # conntrack exits 1 when there was nothing to delete.
        self._ok("conntrack", "-D", "-p", "udp", "--orig-port-dst", str(port))

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

    def _secret(self, name: str, text: str) -> None:
        path = self._folder / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)

    @staticmethod
    def _run(*command: str) -> str:
        return subprocess.run(command, capture_output=True, text=True, check=True).stdout

    @staticmethod
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
