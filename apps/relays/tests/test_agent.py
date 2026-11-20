"""The relay agent on a fake network: it moves a group to a live member without the panel."""

import json
import os
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Interface
from pathlib import Path

import pytest

from amnezia_core.keys import KeyPair
from amnezia_core.relay import Backend as CoreBackend
from amnezia_core.relay import (
    RelayConfig,
    RelayGroup,
    RelayState,
    ServicePeer,
    SwitchOutcome,
    SwitchRequest,
)
from apps.relays.agent.relay_agent import (
    Agent,
    Backend,
    Flow,
    Group,
    Shell,
    parse_counters,
    parse_flows,
    ruleset,
)

A = Backend("203.0.113.10", 55424)
B = Backend("203.0.113.20", 55424)
C = Backend("203.0.113.30", 55424)
PORT = 55424


@dataclass
class FakeNetwork:
    """Members in `alive` answer a probe, the uplink works through those in `through`; every
    call is recorded in order."""

    alive: set[Backend] = field(default_factory=set)
    calls: list[tuple[str, object]] = field(default_factory=list)
    rules: str = ""
    bytes: dict[int, tuple[int, int]] = field(default_factory=dict)
    clients: dict[int, list[Flow]] = field(default_factory=dict)
    through: set[Backend] = field(default_factory=lambda: {A, B, C})
    endpoint: Backend | None = None

    def prepare(self, group: Group) -> None:
        self.calls.append(("prepare", group.port))

    def drop(self, port: int) -> None:
        self.calls.append(("drop", port))

    def probe(self, group: Group, backend: Backend) -> bool:
        self.calls.append(("probe", backend))
        return backend in self.alive

    def forward(self, rules: str) -> None:
        self.calls.append(("forward", None))
        self.rules = rules
        self.bytes = {}

    def forget(self, port: int) -> None:
        self.calls.append(("forget", port))

    def counters(self) -> dict[int, tuple[int, int]]:
        return dict(self.bytes)

    def flows(self, port: int) -> list[Flow]:
        return list(self.clients.get(port, ()))

    def clone(self, group: Group) -> None:
        self.calls.append(("clone", group.port))

    def unclone(self, port: int) -> None:
        self.calls.append(("unclone", port))

    def limit(self, script: Path) -> None:
        self.calls.append(("limit", script.name))

    def point(self, group: Group, backend: Backend) -> None:
        self.calls.append(("point", backend))
        self.endpoint = backend

    def uplink_alive(self, group: Group, target: str) -> bool:
        """The uplink works through the members in `through`."""
        self.calls.append(("uplink", target))
        return self.endpoint in self.through

    def since(self, mark: int) -> list[tuple[str, object]]:
        return self.calls[mark:]


def service() -> ServicePeer:
    return ServicePeer(
        private_key=KeyPair.generate().private_key,
        address=IPv4Interface("10.8.1.250/32"),
        server_public_key=KeyPair.generate().public_key,
        preshared_key=KeyPair.generate().private_key,
        awg_params={"Jc": "4", "S1": "15"},
    )


def config(*groups: tuple[int, tuple[Backend, ...]], failures: int = 3) -> str:
    """A config written by the panel's own model, so agent and panel agree on the format."""
    relay_groups = tuple(
        RelayGroup(
            port,
            tuple(CoreBackend(IPv4Address(b.host), b.port) for b in backends),
            service(),
        )
        for port, backends in groups
    )
    return RelayConfig(relay_groups, failures=failures).to_json()


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        self.now += 5
        return self.now


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    (tmp_path / "config.json").write_text(config((PORT, (A, B, C))))
    return tmp_path


@pytest.fixture
def network() -> FakeNetwork:
    return FakeNetwork(alive={A, B, C})


def agent_for(network: FakeNetwork, folder: Path) -> Agent:
    return Agent(network, folder, clock=Clock(), log=lambda line: None)


def state(folder: Path) -> RelayState:
    return RelayState.parse((folder / "state.json").read_text())


def active(folder: Path) -> CoreBackend | None:
    group = state(folder).group(PORT)
    assert group is not None
    return group.active


def core(backend: Backend) -> CoreBackend:
    return CoreBackend(IPv4Address(backend.host), backend.port)


def test_first_pass_forwards_to_the_first_member(network: FakeNetwork, folder: Path) -> None:
    agent_for(network, folder).tick()

    assert active(folder) == core(A)
    assert f"udp dport {PORT} dnat to {A}" in network.rules
    assert ("prepare", PORT) in network.calls


def test_switches_after_failures_in_a_row_without_the_panel(
    network: FakeNetwork, folder: Path
) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    network.alive.discard(A)

    agent.tick()
    agent.tick()
    assert active(folder) == core(A), "two failures are not enough"
    mark = len(network.calls)
    agent.tick()

    assert active(folder) == core(B)
    calls = network.since(mark)
    # The probe of the round decides, then the rule and conntrack go.
    assert calls.count(("probe", B)) == 1
    assert calls[-2:] == [("forward", None), ("forget", PORT)]
    assert f"dnat to {B}" in network.rules and f"dnat to {A}" not in network.rules
    assert f"ip daddr {B.host} udp dport {B.port} masquerade" in network.rules


def test_skips_a_dead_candidate(network: FakeNetwork, folder: Path) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    network.alive = {C}

    for _ in range(3):
        agent.tick()

    assert active(folder) == core(C)


def test_a_member_dropping_a_handshake_right_after_another_still_takes_the_group(
    folder: Path,
) -> None:
    class Strict(FakeNetwork):
        """Like WireGuard, B drops a handshake that comes right after one it has answered."""

        def probe(self, group: Group, backend: Backend) -> bool:
            repeated = self.calls[-1:] == [("probe", backend)]
            return super().probe(group, backend) and not repeated

    (folder / "config.json").write_text(config((PORT, (A, B))))
    network = Strict(alive={A, B})
    agent = agent_for(network, folder)
    agent.tick()
    network.alive = {B}

    for _ in range(3):
        agent.tick()

    assert active(folder) == core(B)


def test_does_not_go_back_to_a_member_of_higher_priority(
    network: FakeNetwork, folder: Path
) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    network.alive.discard(A)
    for _ in range(3):
        agent.tick()
    network.alive.add(A)
    mark = len(network.calls)

    agent.tick()

    assert active(folder) == core(B)
    assert ("forward", None) not in network.since(mark)


def test_keeps_the_member_when_nobody_answers(network: FakeNetwork, folder: Path) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    network.alive.clear()

    for _ in range(5):
        agent.tick()

    assert active(folder) == core(A)
    group = state(folder).group(PORT)
    assert group is not None
    assert [backend.alive for backend in group.backends] == [False, False, False]
    assert group.backends[0].failures == 5


def test_a_member_removed_by_the_panel_is_left_at_once(network: FakeNetwork, folder: Path) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    (folder / "config.json").write_text(config((PORT, (B, C))))
    os.utime(folder / "config.json", (1, 1))

    agent.tick()

    assert active(folder) == core(B)
    assert ("forget", PORT) in network.calls


def test_a_restarted_agent_stays_on_the_live_member(network: FakeNetwork, folder: Path) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    network.alive.discard(A)
    for _ in range(3):
        agent.tick()
    network.alive.add(A)

    restarted = FakeNetwork(alive={A, B, C})
    agent_for(restarted, folder).tick()

    assert active(folder) == core(B)
    assert f"dnat to {B}" in restarted.rules


def test_a_bad_config_keeps_the_previous_one(network: FakeNetwork, folder: Path) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    (folder / "config.json").write_text(json.dumps({"version": 99}))
    os.utime(folder / "config.json", (1, 1))

    agent.tick()

    assert active(folder) == core(A)


def test_a_group_left_out_of_the_config_is_dropped(network: FakeNetwork, folder: Path) -> None:
    (folder / "config.json").write_text(config((PORT, (A,)), (51820, (B,))))
    agent = agent_for(network, folder)
    agent.tick()
    (folder / "config.json").write_text(config((51820, (B,))))
    os.utime(folder / "config.json", (1, 1))

    agent.tick()

    assert ("drop", PORT) in network.calls
    assert f"dport {PORT} dnat" not in network.rules
    assert [group.port for group in state(folder).groups] == [51820]


def test_traffic_survives_a_rule_replacement(network: FakeNetwork, folder: Path) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    network.bytes = {PORT: (1000, 3000)}
    network.alive.discard(A)
    for _ in range(3):
        agent.tick()
    network.bytes = {PORT: (10, 30)}
    agent.tick()

    group = state(folder).group(PORT)
    assert group is not None
    assert (group.bytes_from_clients, group.bytes_to_clients) == (1010, 3030)


def ask(folder: Path, backend: Backend, *, port: int = PORT, request: str = "r1") -> None:
    """A switch request written by the panel's own model."""
    (folder / "switch.json").write_text(SwitchRequest(request, port, core(backend)).to_json())


def answer(folder: Path) -> tuple[str, SwitchOutcome] | None:
    switch = state(folder).switch
    return (switch.id, switch.outcome) if switch else None


def test_a_switch_asked_by_the_panel_moves_the_group(network: FakeNetwork, folder: Path) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    ask(folder, C)
    mark = len(network.calls)

    agent.tick()

    assert active(folder) == core(C)
    assert answer(folder) == ("r1", SwitchOutcome.SWITCHED)
    assert not (folder / "switch.json").exists(), "a request is done once"
    calls = network.since(mark)
    # The candidate is probed first; the rule and conntrack go as on a failure, once.
    assert calls[0] == ("probe", C)
    assert calls.count(("forget", PORT)) == 1
    assert f"dnat to {C}" in network.rules and f"dnat to {A}" not in network.rules
    group = state(folder).group(PORT)
    assert group is not None and group.switched_at is not None


def test_an_unreachable_candidate_is_refused(network: FakeNetwork, folder: Path) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    network.alive.discard(C)
    ask(folder, C)
    mark = len(network.calls)

    agent.tick()

    assert active(folder) == core(A)
    assert answer(folder) == ("r1", SwitchOutcome.UNREACHABLE)
    assert ("forward", None) not in network.since(mark)


@pytest.mark.parametrize(
    ("backend", "port", "outcome"),
    [
        (A, PORT, SwitchOutcome.ALREADY_ACTIVE),
        (Backend("203.0.113.99", PORT), PORT, SwitchOutcome.UNKNOWN_BACKEND),
        (B, 51820, SwitchOutcome.UNKNOWN_GROUP),
    ],
)
def test_a_switch_that_cannot_be_done_changes_nothing(
    network: FakeNetwork, folder: Path, backend: Backend, port: int, outcome: SwitchOutcome
) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    ask(folder, backend, port=port)
    mark = len(network.calls)

    agent.tick()

    assert active(folder) == core(A)
    assert answer(folder) == ("r1", outcome)
    assert ("forward", None) not in network.since(mark)


def test_the_answer_stays_until_the_next_request(network: FakeNetwork, folder: Path) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    ask(folder, B)
    agent.tick()
    agent.tick()
    assert answer(folder) == ("r1", SwitchOutcome.SWITCHED)

    ask(folder, C, request="r2")
    agent.tick()

    assert answer(folder) == ("r2", SwitchOutcome.SWITCHED)
    assert active(folder) == core(C)


def test_a_bad_request_is_dropped(network: FakeNetwork, folder: Path) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    (folder / "switch.json").write_text(json.dumps({"version": 99, "id": "x"}))

    agent.tick()

    assert active(folder) == core(A)
    assert answer(folder) is None
    assert not (folder / "switch.json").exists()


def test_after_a_planned_switch_the_agent_still_fails_over_on_its_own(
    network: FakeNetwork, folder: Path
) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    ask(folder, C)
    agent.tick()
    network.alive.discard(C)

    for _ in range(3):
        agent.tick()

    assert active(folder) == core(A)


def unanswered(folder: Path) -> bool:
    group = state(folder).group(PORT)
    assert group is not None
    return group.unanswered


def waiting(*clients: str, member: Backend = A) -> list[Flow]:
    return [Flow(client, member.host, answered=False) for client in clients]


def test_clients_without_an_answer_round_after_round_are_told(
    network: FakeNetwork, folder: Path
) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    network.clients = {PORT: waiting("198.51.100.1", "198.51.100.2")}

    agent.tick()
    agent.tick()
    assert not unanswered(folder), "two rounds are not enough"
    agent.tick()

    assert unanswered(folder)
    assert active(folder) == core(A), "the agent does not switch on it"


@pytest.mark.parametrize(
    "flows",
    [
        waiting("198.51.100.1"),
        waiting("198.51.100.1", "198.51.100.1"),
        [*waiting("198.51.100.1", "198.51.100.2"), Flow("198.51.100.3", A.host, answered=True)],
        waiting("198.51.100.1", "198.51.100.2", member=B),
    ],
    ids=["one client", "one client twice", "another client answered", "an old member"],
)
def test_waiting_clients_are_not_enough_alone(
    network: FakeNetwork, folder: Path, flows: list[Flow]
) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    network.clients = {PORT: flows}

    for _ in range(5):
        agent.tick()

    assert not unanswered(folder)


def test_a_switch_starts_counting_again(network: FakeNetwork, folder: Path) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    network.clients = {PORT: waiting("198.51.100.1", "198.51.100.2")}
    for _ in range(3):
        agent.tick()
    assert unanswered(folder)

    ask(folder, B)
    agent.tick()

    assert active(folder) == core(B)
    assert not unanswered(folder)


def test_parse_flows_keeps_the_clients_and_leaves_out_the_probes() -> None:
    text = "\n".join(
        [
            "udp      17 119 src=172.17.0.2 dst=203.0.113.20 sport=45506 dport=55424 "
            "src=203.0.113.20 dst=172.17.0.2 sport=55424 dport=45506 [ASSURED] mark=0 use=1",
            "udp      17 29 src=198.51.100.1 dst=172.17.0.2 sport=40001 dport=55424 [UNREPLIED] "
            "src=203.0.113.20 dst=172.17.0.2 sport=55424 dport=40001 mark=0 use=1",
            "udp      17 170 src=198.51.100.2 dst=172.17.0.2 sport=40002 dport=55424 "
            "src=203.0.113.10 dst=172.17.0.2 sport=55424 dport=40002 [ASSURED] mark=0 use=1",
            "conntrack v1.4.7 (conntrack-tools): 3 flow entries have been shown.",
        ]
    )

    assert parse_flows(text) == [
        Flow("198.51.100.1", "203.0.113.20", answered=False),
        Flow("198.51.100.2", "203.0.113.10", answered=True),
    ]


def test_a_probe_binds_a_new_source_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A filter may pass a flow it knows and drop new ones: each probe is a new flow."""
    commands: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = "key\t1700000000\n"

    def run(command: list[str], **_kwargs: object) -> Done:
        commands.append(list(command))
        return Done()

    monkeypatch.setattr("subprocess.run", run)
    group = Group(PORT, (A,), {"server_public_key": "key"})

    assert Shell(tmp_path).probe(group, A)

    added = next(command for command in commands if "endpoint" in command)
    assert added[added.index("listen-port") + 1] == "0"


def test_ruleset_replaces_the_table_in_one_transaction() -> None:
    rules = ruleset({PORT: A, 51820: None})

    lines = rules.splitlines()
    assert lines[:3] == [
        "table ip lamnezia_relay",
        "delete table ip lamnezia_relay",
        "table ip lamnezia_relay {",
    ]
    assert "\tcounter from_clients_51820 { }" in lines
    assert "dport 51820 dnat" not in rules
    assert f'ip saddr {A.host} udp sport {A.port} counter name "to_clients_{PORT}"' in rules


def test_parse_counters_reads_nft_json() -> None:
    text = json.dumps(
        {
            "nftables": [
                {"metainfo": {"version": "1.0.9"}},
                {"counter": {"name": f"from_clients_{PORT}", "packets": 3, "bytes": 120}},
                {"counter": {"name": f"to_clients_{PORT}", "packets": 2, "bytes": 80}},
                {"counter": {"name": "other", "bytes": 5}},
            ]
        }
    )

    assert parse_counters(text) == {PORT: (120, 80)}
