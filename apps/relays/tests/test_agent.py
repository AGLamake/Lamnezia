"""The relay agent on a fake network: it moves a group to a live member without the panel."""

import json
import os
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Interface
from pathlib import Path

import pytest

from amnezia_core.keys import KeyPair
from amnezia_core.relay import Backend as CoreBackend
from amnezia_core.relay import RelayConfig, RelayGroup, RelayState, ServicePeer
from apps.relays.agent.relay_agent import Agent, Backend, Group, parse_counters, ruleset

A = Backend("203.0.113.10", 55424)
B = Backend("203.0.113.20", 55424)
C = Backend("203.0.113.30", 55424)
PORT = 55424


@dataclass
class FakeNetwork:
    """Members in `alive` answer a probe; every call is recorded in order."""

    alive: set[Backend] = field(default_factory=set)
    calls: list[tuple[str, object]] = field(default_factory=list)
    rules: str = ""
    bytes: dict[int, tuple[int, int]] = field(default_factory=dict)

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
    # The candidate is probed again right before the switch, then the rule and conntrack go.
    probes_of_b = [index for index, call in enumerate(calls) if call == ("probe", B)]
    assert len(probes_of_b) == 2
    assert calls[probes_of_b[1] + 1 :] == [("forward", None), ("forget", PORT)]
    assert f"dnat to {B}" in network.rules and f"dnat to {A}" not in network.rules
    assert f"ip daddr {B.host} udp dport {B.port} masquerade" in network.rules


def test_skips_a_dead_candidate(network: FakeNetwork, folder: Path) -> None:
    agent = agent_for(network, folder)
    agent.tick()
    network.alive = {C}

    for _ in range(3):
        agent.tick()

    assert active(folder) == core(C)


def test_a_candidate_failing_the_second_probe_is_not_taken(folder: Path) -> None:
    class Flaky(FakeNetwork):
        """B answers the regular probe, made after A, but not the one before the switch."""

        def probe(self, group: Group, backend: Backend) -> bool:
            confirming = backend == B and self.calls[-1] != ("probe", A)
            return super().probe(group, backend) and not confirming

    network = Flaky(alive={A, B})
    agent = agent_for(network, folder)
    agent.tick()
    network.alive = {B}
    mark = len(network.calls)

    for _ in range(3):
        agent.tick()

    assert active(folder) == core(A)
    assert ("forget", PORT) not in network.since(mark)


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
