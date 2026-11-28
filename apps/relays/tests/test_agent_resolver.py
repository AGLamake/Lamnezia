"""The DNS resolver of a split group's clients on the relay and the redirect of their queries."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from amnezia_core.relay import RelayState, ResolverState, ResolverStatus, Routing
from apps.relays.agent.relay_agent import (
    RESOLVER_ERROR_LIMIT,
    RESOLVER_RETRY,
    Resolver,
    resolver_config,
    start_unbound,
)
from apps.relays.tests.test_agent import PORT, A, B, C, FakeNetwork
from apps.relays.tests.test_agent_split import (
    SUBNET,
    FakeProcess,
    Unbound,
    started,
    until_forward,
    write_config,
)

UDP = f'iifname "lg{PORT}" ip daddr {{ 1.1.1.1, 1.0.0.1 }} udp dport 53 redirect to :53'
TCP = f'iifname "lg{PORT}" ip daddr {{ 1.1.1.1, 1.0.0.1 }} tcp dport 53 redirect to :53'


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    write_config(tmp_path)
    return tmp_path


@pytest.fixture
def network() -> FakeNetwork:
    return FakeNetwork(alive={A, B, C})


@pytest.fixture
def unbound() -> Unbound:
    return Unbound()


def test_the_queries_to_the_key_dns_go_to_the_resolver(
    network: FakeNetwork, folder: Path, unbound: Unbound
) -> None:
    started(network, folder, unbound)

    assert UDP in network.rules and TCP in network.rules
    (config, process), *_ = unbound.started
    assert unbound.running == [process]
    assert f"access-control: {SUBNET} allow" in config


def test_a_forwarded_group_has_no_redirect_and_no_resolver(
    network: FakeNetwork, folder: Path, unbound: Unbound
) -> None:
    write_config(folder, Routing.FORWARD)

    started(network, folder, unbound)

    assert "dport 53" not in network.rules
    assert not unbound.started


def test_the_emergency_forward_stops_the_resolver(
    network: FakeNetwork, folder: Path, unbound: Unbound
) -> None:
    agent = started(network, folder, unbound)

    until_forward(agent, folder, network)

    assert "dport 53" not in network.rules
    assert not unbound.running


def test_a_group_moved_back_to_forward_stops_the_resolver(
    network: FakeNetwork, folder: Path, unbound: Unbound
) -> None:
    agent = started(network, folder, unbound)
    write_config(folder, Routing.FORWARD, moment=2)

    agent.tick()

    assert "dport 53" not in network.rules
    assert not unbound.running


def test_without_unbound_the_dns_stays_as_it_was(network: FakeNetwork, folder: Path) -> None:
    started(network, folder, Unbound(missing=True))

    assert "dport 53" not in network.rules
    assert f"lg{PORT}" in network.rules


def test_a_stopped_resolver_is_started_again(
    network: FakeNetwork, folder: Path, unbound: Unbound
) -> None:
    agent = started(network, folder, unbound)
    unbound.running[0].code = 1

    agent.tick()
    assert "dport 53" not in network.rules
    for _ in range(RESOLVER_RETRY):
        agent.tick()
        if unbound.running:
            break

    assert len(unbound.started) == 2 and UDP in network.rules


def test_the_config_logs_no_query_or_answer() -> None:
    lines = [line.strip() for line in resolver_config([SUBNET]).splitlines()]

    for setting in ("log-queries", "log-replies", "log-servfail", "log-local-actions"):
        assert f"{setting}: no" in lines
    assert 'logfile: "/dev/null"' in lines
    assert "use-syslog: no" in lines and "verbosity: 0" in lines
    assert not [line for line in lines if line.startswith("log") and line.endswith("yes")]


def test_the_config_asks_over_tls_and_answers_only_the_clients() -> None:
    lines = [line.strip() for line in resolver_config([SUBNET, SUBNET]).splitlines()]

    assert "forward-tls-upstream: yes" in lines
    upstreams = [line for line in lines if line.startswith("forward-addr:")]
    assert upstreams and all(line.endswith("@853#cloudflare-dns.com") for line in upstreams)
    access = [line for line in lines if line.startswith("access-control:")]
    assert access == ["access-control: 0.0.0.0/0 refuse", f"access-control: {SUBNET} allow"]


def test_the_resolver_restarts_only_for_other_subnets(tmp_path: Path, unbound: Unbound) -> None:
    resolver = Resolver(tmp_path, Clock(), lambda line: None, unbound)

    assert resolver.serve([SUBNET]) and resolver.serve([SUBNET])
    assert len(unbound.started) == 1
    assert resolver.serve([SUBNET, "10.8.2.0/24"])

    assert len(unbound.started) == 2 and len(unbound.running) == 1
    assert not resolver.serve([]) and not unbound.running


def test_a_resolver_that_does_not_start_is_tried_again_later(tmp_path: Path) -> None:
    clock, log, unbound = Clock(), list[str](), Unbound(missing=True)
    resolver = Resolver(tmp_path, clock, log.append, unbound)

    assert not resolver.serve([SUBNET])
    assert not resolver.stale()
    clock.now += RESOLVER_RETRY
    assert resolver.stale() and not resolver.serve([SUBNET])
    unbound.missing = False
    clock.now += RESOLVER_RETRY

    assert resolver.serve([SUBNET])
    assert len(log) == 1 and "resolver not started" in log[0]


def test_unbound_runs_with_its_output_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    def popen(command: list[str], **kwargs: Any) -> object:
        calls.append((command, kwargs))
        return object()

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)

    start_unbound(tmp_path / "resolver.conf")

    (check, _), (command, options) = calls
    assert check[0] == "unbound-checkconf"
    assert command[:3] == ["unbound", "-d", "-p"]
    assert options["stdout"] == options["stderr"] == subprocess.DEVNULL


def test_a_bad_config_is_not_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "syntax error", "")

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(OSError, match="syntax error"):
        start_unbound(tmp_path / "resolver.conf")


def resolver_state(folder: Path) -> ResolverState:
    return RelayState.parse((folder / "state.json").read_text()).resolver


def test_the_state_tells_a_running_resolver(
    network: FakeNetwork, folder: Path, unbound: Unbound
) -> None:
    started(network, folder, unbound)

    assert resolver_state(folder) == ResolverState(ResolverStatus.RUNNING)


def test_the_state_tells_no_resolver_without_split_groups(
    network: FakeNetwork, folder: Path, unbound: Unbound
) -> None:
    write_config(folder, Routing.FORWARD)

    started(network, folder, unbound)

    assert resolver_state(folder) == ResolverState(ResolverStatus.OFF)


def test_the_state_tells_an_image_without_unbound(network: FakeNetwork, folder: Path) -> None:
    started(network, folder, Unbound(missing=True))

    assert resolver_state(folder) == ResolverState(ResolverStatus.MISSING)


def test_the_state_tells_why_the_resolver_failed(
    network: FakeNetwork, folder: Path, unbound: Unbound
) -> None:
    agent = started(network, folder, unbound)
    unbound.running[0].code = 1

    agent.tick()

    assert resolver_state(folder) == ResolverState(
        ResolverStatus.FAILED, "resolver stopped with code 1"
    )


def test_a_long_failure_is_cut_in_the_state(tmp_path: Path) -> None:
    def start(config: Path) -> FakeProcess:
        raise OSError("bad config: " + "x" * 1000)

    resolver = Resolver(tmp_path, Clock(), lambda line: None, start)
    resolver.serve([SUBNET])

    state = resolver.state()
    assert state["status"] == "failed"
    assert len(state["error"]) == RESOLVER_ERROR_LIMIT


def test_a_started_resolver_is_no_longer_missing(tmp_path: Path) -> None:
    clock, unbound = Clock(), Unbound(missing=True)
    resolver = Resolver(tmp_path, clock, lambda line: None, unbound)
    resolver.serve([SUBNET])
    unbound.missing = False
    clock.now += RESOLVER_RETRY

    resolver.serve([SUBNET])

    assert resolver.state() == {"status": "running"}
