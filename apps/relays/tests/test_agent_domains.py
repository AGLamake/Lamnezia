"""The relay agent with own domains of a split group: the lookups and the sets they fill."""

from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest

from amnezia_core.relay import RelayState
from apps.relays.agent.relay_agent import Agent, Domains, ResolverError, parse_domains
from apps.relays.tests.test_agent import PORT, A, B, C, Clock, FakeNetwork
from apps.relays.tests.test_agent_split import write_config, write_file

DOMAINS = f"domains-{PORT}.txt"


class Resolver:
    """Lookups that finish at once, or only when `finish` is called if `held`."""

    def __init__(self, answers: dict[str, tuple[str, ...]]) -> None:
        self.answers = answers
        self.asked: list[str] = []
        self.held = False
        self.down = False
        self._waiting: list[tuple[Future[Any], str]] = []

    def submit(self, fn: Callable[[str], tuple[str, ...]], name: str, /) -> Future[Any]:
        self.asked.append(name)
        future: Future[Any] = Future()
        if self.held:
            self._waiting.append((future, name))
        else:
            self._answer(future, name)
        return future

    def finish(self) -> None:
        for future, name in self._waiting:
            self._answer(future, name)
        self._waiting = []

    def lookup(self, name: str) -> tuple[str, ...]:
        if self.down:
            raise ResolverError("the resolver does not answer: timed out")
        if name not in self.answers:
            raise OSError("no such name")
        return self.answers[name]

    def _answer(self, future: Future[Any], name: str) -> None:
        try:
            future.set_result(self.lookup(name))
        except (OSError, ResolverError) as error:
            future.set_exception(error)


class Moment:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def resolver() -> Resolver:
    return Resolver({"example.ru": ("93.184.215.14",), "video.example": ("5.8.1.1", "5.8.1.2")})


def domains(
    resolver: Resolver, clock: Callable[[], float], ready: Callable[[], bool] = lambda: True
) -> Domains:
    return Domains(clock, lookup=resolver.lookup, pool=resolver, interval=600, ready=ready)


def test_parse_domains_splits_the_directions() -> None:
    text = "local example.ru\n# own\nabroad video.example\n\nlocal example.ru\n"

    assert parse_domains(text) == (("example.ru",), ("video.example",))


@pytest.mark.parametrize("text", ["example.ru\n", "elsewhere example.ru\n", "local a b\n"])
def test_parse_domains_refuses_a_bad_line(text: str) -> None:
    with pytest.raises(ValueError, match="line 1"):
        parse_domains(text)


def test_new_domains_are_looked_up_at_once(resolver: Resolver) -> None:
    found = domains(resolver, Moment())
    found.set(PORT, ("example.ru",), ("video.example",))

    assert found.poll()
    assert found.addresses(PORT) == (("93.184.215.14",), ("5.8.1.1", "5.8.1.2"))


def test_domains_are_looked_up_again_after_the_interval(resolver: Resolver) -> None:
    clock = Moment()
    found = domains(resolver, clock)
    found.set(PORT, ("example.ru",), ())
    found.poll()
    resolver.answers["example.ru"] = ("93.184.215.15",)

    clock.now += 599
    assert not found.poll()
    clock.now += 1
    assert found.poll()
    assert found.addresses(PORT) == (("93.184.215.15",), ())
    assert resolver.asked == ["example.ru", "example.ru"]


def test_a_name_that_fails_keeps_its_addresses(resolver: Resolver) -> None:
    clock = Moment()
    found = domains(resolver, clock)
    found.set(PORT, ("example.ru",), ())
    found.poll()
    del resolver.answers["example.ru"]

    clock.now += 600
    assert not found.poll()
    assert found.addresses(PORT) == (("93.184.215.14",), ())


def test_a_failed_resolver_is_told_and_the_addresses_stay(resolver: Resolver) -> None:
    clock = Moment()
    found = domains(resolver, clock)
    found.set(PORT, ("example.ru",), ("video.example",))
    found.poll()
    resolver.down = True

    clock.now += 600
    assert not found.poll()
    assert found.addresses(PORT) == (("93.184.215.14",), ("5.8.1.1", "5.8.1.2"))
    assert found.failure == "2 of 2 lookups failed: the resolver does not answer: timed out"
    assert "example" not in found.failure


def test_a_failed_round_is_tried_again_soon(resolver: Resolver) -> None:
    clock = Moment()
    found = domains(resolver, clock)
    resolver.down = True
    found.set(PORT, ("example.ru",), ())
    found.poll()
    resolver.down = False

    clock.now += 59
    assert not found.poll()
    clock.now += 1
    assert found.poll()
    assert found.addresses(PORT) == (("93.184.215.14",), ())
    assert found.failure == ""


def test_a_name_that_does_not_exist_is_no_failure(resolver: Resolver) -> None:
    found = domains(resolver, Moment())
    found.set(PORT, ("gone.example",), ())
    found.poll()

    assert found.failure == ""


def test_no_lookups_while_the_resolver_is_not_ready(resolver: Resolver) -> None:
    ready = False
    found = domains(resolver, Moment(), lambda: ready)
    found.set(PORT, ("example.ru",), ())

    assert not found.poll()
    assert resolver.asked == []
    ready = True
    assert found.poll()
    assert resolver.asked == ["example.ru"]


def test_a_slow_lookup_does_not_hold_up_the_poll(resolver: Resolver) -> None:
    found = domains(resolver, Moment())
    resolver.held = True
    found.set(PORT, ("example.ru",), ())

    assert not found.poll()
    assert found.addresses(PORT) == ((), ())
    resolver.finish()
    assert found.poll()
    assert found.addresses(PORT) == (("93.184.215.14",), ())


def test_known_names_need_no_new_round(resolver: Resolver) -> None:
    found = domains(resolver, Moment())
    found.set(PORT, ("example.ru",), ("video.example",))
    found.poll()

    assert found.set(PORT, ("video.example",), ("example.ru",))
    assert not found.poll()
    assert found.addresses(PORT) == (("5.8.1.1", "5.8.1.2"), ("93.184.215.14",))
    assert len(resolver.asked) == 2


def test_dropped_names_forget_their_addresses(resolver: Resolver) -> None:
    clock = Moment()
    found = domains(resolver, clock)
    found.set(PORT, ("example.ru",), ())
    found.poll()
    found.drop(PORT)
    found.set(9733, ("video.example",), ())
    found.poll()

    found.set(PORT, ("example.ru",), ())
    assert found.addresses(PORT) == ((), ())


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    write_config(tmp_path)
    return tmp_path


@pytest.fixture
def network() -> FakeNetwork:
    return FakeNetwork(alive={A, B, C})


def agent(network: FakeNetwork, folder: Path, resolver: Resolver, log: list[str]) -> Agent:
    clock = Clock()
    started = Agent(network, folder, clock, log.append, domains=domains(resolver, clock))
    started.tick()
    return started


def test_domain_addresses_reach_the_sets(
    network: FakeNetwork, folder: Path, resolver: Resolver
) -> None:
    log: list[str] = []
    relay = agent(network, folder, resolver, log)
    write_file(folder, DOMAINS, "local example.ru\nabroad video.example\n", 10)
    mark = len(network.calls)

    relay.tick()

    assert network.since(mark).count(("forward", None)) == 1
    assert f"set dl_{PORT} {{" in network.rules and "93.184.215.14" in network.rules
    assert "elements = { 5.8.1.1,\n\t\t\t5.8.1.2 }" in network.rules
    assert f"ip daddr @da_{PORT} meta mark set" in network.rules
    assert not any("example" in line for line in log)


def test_a_broken_domain_list_keeps_the_domains(
    network: FakeNetwork, folder: Path, resolver: Resolver
) -> None:
    log: list[str] = []
    relay = agent(network, folder, resolver, log)
    write_file(folder, DOMAINS, "local example.ru\n", 10)
    relay.tick()
    write_file(folder, DOMAINS, "local example.ru\nnot a line at all\n", 20)

    relay.tick()

    assert "93.184.215.14" in network.rules
    assert any("domain list ignored" in line for line in log)


def test_a_removed_domain_list_empties_the_sets(
    network: FakeNetwork, folder: Path, resolver: Resolver
) -> None:
    relay = agent(network, folder, resolver, [])
    write_file(folder, DOMAINS, "local example.ru\n", 10)
    relay.tick()
    (folder / DOMAINS).unlink()

    relay.tick()

    assert "93.184.215.14" not in network.rules


def test_a_removed_network_list_empties_the_set(
    network: FakeNetwork, folder: Path, resolver: Resolver
) -> None:
    relay = agent(network, folder, resolver, [])
    write_file(folder, f"networks-{PORT}.txt", "5.8.0.0/16\n", 10)
    relay.tick()
    (folder / f"networks-{PORT}.txt").unlink()

    relay.tick()

    assert "5.8.0.0/16" not in network.rules


def test_the_state_tells_why_the_lookups_failed(
    network: FakeNetwork, folder: Path, resolver: Resolver
) -> None:
    log: list[str] = []
    relay = agent(network, folder, resolver, log)
    resolver.down = True
    write_file(folder, DOMAINS, "local example.ru\n", 10)

    relay.tick()

    state = RelayState.parse((folder / "state.json").read_text()).resolver
    assert state.lookup_error == "1 of 1 lookups failed: the resolver does not answer: timed out"
    assert not any("example" in line for line in log)
