"""The relay agent with a split group: the clone, the uplink and the emergency forward."""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Interface
from pathlib import Path
from typing import NoReturn

import pytest

from amnezia_core.keys import KeyPair
from amnezia_core.relay import (
    Clone,
    GroupState,
    RelayConfig,
    RelayGroup,
    RelayState,
    Routing,
    ServicePeer,
    SplitRouting,
)
from amnezia_core.split_lists import ListMode
from apps.relays.agent.relay_agent import (
    MARK_BASE,
    UPLINK_SILENCE,
    Agent,
    Backend,
    Resolver,
    Settings,
    SplitRoute,
    parse_networks,
    ruleset,
)
from apps.relays.tests.test_agent import PORT, A, B, C, Clock, FakeNetwork, core, service

SUBNET = "10.8.1.0/24"
DNS = ("1.1.1.1", "1.0.0.1")


@dataclass
class FakeProcess:
    """A resolver process: runs until `code` is set."""

    code: int | None = None

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.code = 0

    def kill(self) -> None:
        self.code = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.code or 0


@dataclass
class Unbound:
    """Starts fake resolver processes with the config they got; raises while `missing`."""

    missing: bool = False
    started: list[tuple[str, FakeProcess]] = field(default_factory=list)

    def __call__(self, config: Path) -> FakeProcess:
        if self.missing:
            raise FileNotFoundError("unbound-checkconf")
        process = FakeProcess()
        self.started.append((config.read_text(), process))
        return process

    @property
    def running(self) -> list[FakeProcess]:
        return [process for _, process in self.started if process.code is None]


def group(
    routing: Routing = Routing.SPLIT,
    list_mode: ListMode = ListMode.EXCLUDE,
    *,
    dns_all: bool = False,
) -> RelayGroup:
    backends = tuple(core(backend) for backend in (A, B, C))
    if routing == Routing.FORWARD:
        return RelayGroup(PORT, backends, service())
    uplink = ServicePeer(
        private_key=KeyPair.generate().private_key,
        address=IPv4Interface("10.8.1.253/32"),
        server_public_key=KeyPair.generate().public_key,
        preshared_key=KeyPair.generate().private_key,
        awg_params={"Jc": "4"},
    )
    clone = Clone(KeyPair.generate().private_key, IPv4Interface("10.8.1.1/24"), {"Jc": "4"})
    dns = tuple(map(IPv4Address, DNS))
    split = SplitRouting(clone, uplink, dns=dns, list_mode=list_mode, dns_all=dns_all)
    return RelayGroup(PORT, backends, service(), routing, split)


def write_config(
    folder: Path,
    routing: Routing = Routing.SPLIT,
    moment: int = 1,
    list_mode: ListMode = ListMode.EXCLUDE,
    *,
    dns_all: bool = False,
) -> None:
    config = RelayConfig((group(routing, list_mode, dns_all=dns_all),), failures=1, recovery=2)
    (folder / "config.json").write_text(config.to_json())
    os.utime(folder / "config.json", (moment, moment))


def write_file(folder: Path, name: str, text: str, moment: int) -> None:
    (folder / name).write_text(text)
    os.utime(folder / name, (moment, moment))


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    write_config(tmp_path)
    return tmp_path


@pytest.fixture
def network() -> FakeNetwork:
    return FakeNetwork(alive={A, B, C})


def started(network: FakeNetwork, folder: Path, unbound: Unbound | None = None) -> Agent:
    clock, log = Clock(), lambda line: None
    resolver = Resolver(folder, clock, log, unbound if unbound is not None else Unbound())
    agent = Agent(network, folder, clock=clock, log=log, resolver=resolver)
    agent.tick()
    return agent


def split_state(folder: Path) -> GroupState:
    found = RelayState.parse((folder / "state.json").read_text()).group(PORT)
    assert found is not None
    return found


def moment(folder: Path) -> float:
    """When the agent wrote its last state."""
    return RelayState.parse((folder / "state.json").read_text()).updated_at.timestamp()


def until_silent(agent: Agent, folder: Path, network: FakeNetwork) -> None:
    """The uplink fails until it has gone unanswered long enough to leave its member; every
    tick before that last one keeps the member."""
    endpoint = network.endpoint
    agent.tick()
    first = moment(folder)
    while moment(folder) - first < UPLINK_SILENCE:
        assert network.endpoint == endpoint, "the uplink left before it was silent long enough"
        agent.tick()


def nearly_silent(agent: Agent, folder: Path) -> None:
    """The uplink fails for a while, short of the silence that makes it leave."""
    agent.tick()
    first = moment(folder)
    agent.tick()
    assert moment(folder) - first < UPLINK_SILENCE


def until_forward(agent: Agent, folder: Path, network: FakeNetwork) -> None:
    """The uplink fails through every member while they all answer probes."""
    network.through = set()
    for _ in range(3):
        until_silent(agent, folder, network)


def test_a_split_group_listens_on_its_clone_without_dnat(
    network: FakeNetwork, folder: Path
) -> None:
    started(network, folder)

    assert ("clone", PORT) in network.calls
    assert ("point", A) in network.calls
    assert "dnat" not in network.rules
    assert f'iifname "lg{PORT}" ip daddr != @local ip daddr != @ru_{PORT}' in network.rules
    assert f"meta mark set {MARK_BASE + PORT}" in network.rules
    state = split_state(folder)
    assert (state.routing, state.mode, state.emergency) == (Routing.SPLIT, Routing.SPLIT, False)


def test_the_uplink_is_checked_through_the_active_member(
    network: FakeNetwork, folder: Path
) -> None:
    agent = started(network, folder)

    agent.tick()

    uplink = split_state(folder).uplink
    assert uplink is not None and uplink.alive
    assert uplink.endpoint == core(A)
    assert ("uplink", "1.1.1.1") in network.calls


def test_a_new_active_member_only_points_the_uplink(network: FakeNetwork, folder: Path) -> None:
    agent = started(network, folder)
    network.alive.discard(A)
    mark = len(network.calls)

    agent.tick()

    calls = network.since(mark)
    assert ("point", B) in calls
    assert ("forward", None) not in calls and ("forget", PORT) not in calls
    assert split_state(folder).active == core(B)


def test_a_failed_uplink_goes_to_the_next_member(network: FakeNetwork, folder: Path) -> None:
    agent = started(network, folder)
    network.through = {B, C}
    mark = len(network.calls)

    until_silent(agent, folder, network)

    calls = network.since(mark)
    assert ("point", B) in calls and ("forget", PORT) not in calls
    state = split_state(folder)
    assert state.active == core(B) and state.mode == Routing.SPLIT


def test_a_lossy_uplink_stays_on_its_member(network: FakeNetwork, folder: Path) -> None:
    agent = started(network, folder)
    chosen = split_state(folder).switched_at
    mark = len(network.calls)

    for _ in range(5):
        network.through = set()
        nearly_silent(agent, folder)
        network.through = {A, B, C}
        agent.tick()

    assert ("point", B) not in network.since(mark)
    state = split_state(folder)
    assert state.active == core(A) and state.switched_at == chosen


def test_a_lossy_uplink_on_every_member_does_not_bounce_back(
    network: FakeNetwork, folder: Path
) -> None:
    agent = started(network, folder)
    network.through = set()
    until_silent(agent, folder, network)
    assert split_state(folder).active == core(B)
    network.through = {A, B, C}
    agent.tick()  # one good check through B: A is not forgiven yet
    network.through = set()
    mark = len(network.calls)

    until_silent(agent, folder, network)

    assert ("point", A) not in network.since(mark)
    assert split_state(folder).active == core(C)


def test_the_group_is_forwarded_when_only_the_uplink_fails(
    network: FakeNetwork, folder: Path
) -> None:
    agent = started(network, folder)

    until_forward(agent, folder, network)

    assert f"udp dport {PORT} dnat to {C}" in network.rules
    assert "meta mark" not in network.rules
    assert network.calls[-1] == ("forget", PORT)
    state = split_state(folder)
    assert state.emergency and state.mode_changed_at is not None


def test_a_group_nobody_answers_stays_split(network: FakeNetwork, folder: Path) -> None:
    agent = started(network, folder)
    network.alive.clear()
    network.through = set()

    for _ in range(5):
        agent.tick()

    assert "dnat" not in network.rules
    assert split_state(folder).mode == Routing.SPLIT


def test_the_group_splits_again_after_good_checks_in_a_row(
    network: FakeNetwork, folder: Path
) -> None:
    agent = started(network, folder)
    until_forward(agent, folder, network)
    network.through = {A}

    agent.tick()  # the uplink fails through C and goes to A
    agent.tick()
    assert split_state(folder).emergency, "one good check is not enough"
    agent.tick()

    state = split_state(folder)
    assert state.mode == Routing.SPLIT and state.active == core(A)
    assert "dnat" not in network.rules
    assert network.calls[-1] == ("forget", PORT)


def test_a_restarted_agent_stays_in_the_emergency_forward(
    network: FakeNetwork, folder: Path
) -> None:
    agent = started(network, folder)
    until_forward(agent, folder, network)

    restarted = FakeNetwork(alive={A, B, C})
    started(restarted, folder)

    assert f"udp dport {PORT} dnat to {C}" in restarted.rules
    assert split_state(folder).emergency


def test_a_network_list_is_applied_in_one_transaction(network: FakeNetwork, folder: Path) -> None:
    agent = started(network, folder)
    text = "# ru\n5.8.0.0/16\n\n5.9.0.0/16  # merged\n"
    write_file(folder, f"networks-{PORT}.txt", text, 10)
    mark = len(network.calls)

    agent.tick()

    assert network.since(mark).count(("forward", None)) == 1
    assert "elements = { 5.8.0.0/15 }" in network.rules
    assert split_state(folder).networks == 1


@pytest.mark.parametrize("text", ["", "# nothing\n", "5.8.0.0/16\nnot a network\n"])
def test_an_empty_or_broken_list_is_not_applied(
    network: FakeNetwork, folder: Path, text: str
) -> None:
    agent = started(network, folder)
    write_file(folder, f"networks-{PORT}.txt", "5.8.0.0/16\n", 10)
    agent.tick()
    write_file(folder, f"networks-{PORT}.txt", text, 20)
    mark = len(network.calls)

    agent.tick()

    assert ("forward", None) not in network.since(mark)
    assert "5.8.0.0/16" in network.rules


def test_new_peers_refresh_the_clone(network: FakeNetwork, folder: Path) -> None:
    agent = started(network, folder)
    write_file(folder, f"peers-{PORT}.conf", "[Peer]\n", 10)
    mark = len(network.calls)

    agent.tick()

    calls = network.since(mark)
    assert calls[:2] == [("clone", PORT), ("point", A)]
    assert ("forget", PORT) not in calls


def test_the_speed_limits_go_on_after_every_clone(network: FakeNetwork, folder: Path) -> None:
    write_file(folder, f"speed-{PORT}.sh", "#!/bin/sh\n", 1)
    agent = started(network, folder)
    write_file(folder, f"peers-{PORT}.conf", "[Peer]\n", 10)
    mark = len(network.calls)

    agent.tick()

    calls = network.since(mark)
    assert calls.index(("clone", PORT)) < calls.index(("limit", f"speed-{PORT}.sh"))


def test_new_speed_limits_keep_the_clone(network: FakeNetwork, folder: Path) -> None:
    agent = started(network, folder)
    write_file(folder, f"speed-{PORT}.sh", "#!/bin/sh\n", 10)
    mark = len(network.calls)

    agent.tick()

    calls = network.since(mark)
    assert ("limit", f"speed-{PORT}.sh") in calls and ("clone", PORT) not in calls


def test_a_clone_without_speed_limits_runs_no_script(network: FakeNetwork, folder: Path) -> None:
    started(network, folder)

    assert not any(name == "limit" for name, _ in network.calls)


def test_a_group_moved_back_to_forward_drops_its_clone(network: FakeNetwork, folder: Path) -> None:
    agent = started(network, folder)
    write_config(folder, Routing.FORWARD, moment=2)
    mark = len(network.calls)

    agent.tick()

    calls = network.since(mark)
    assert ("unclone", PORT) in calls and calls[-1] == ("forget", PORT)
    assert f"udp dport {PORT} dnat to {A}" in network.rules
    assert split_state(folder).routing == Routing.FORWARD


def test_a_forwarded_group_moved_to_split_gets_its_clone(
    network: FakeNetwork, folder: Path
) -> None:
    write_config(folder, Routing.FORWARD)
    agent = started(network, folder)
    write_config(folder, Routing.SPLIT, moment=2)

    agent.tick()

    assert ("clone", PORT) in network.calls and network.calls[-1] == ("forget", PORT)
    assert "dnat" not in network.rules


def test_split_ruleset_marks_all_but_the_list_and_local_networks() -> None:
    rules = ruleset({}, [SplitRoute(PORT, SUBNET, ("5.8.0.0/15",))])

    assert "\tset local {" in rules and "192.168.0.0/16" in rules
    assert f"\tset ru_{PORT} {{" in rules and "elements = { 5.8.0.0/15 }" in rules
    assert "type filter hook prerouting priority mangle" in rules
    assert f'ip saddr {SUBNET} oifname != "lg{PORT}" masquerade' in rules
    assert f'iifname "lg{PORT}" counter name "from_clients_{PORT}"' in rules
    assert f'oifname "lg{PORT}" counter name "to_clients_{PORT}"' in rules
    assert f'oifname "lu{PORT}" tcp flags syn tcp option maxseg size set rt mtu' in rules
    assert "dnat" not in rules


def test_split_ruleset_counts_what_goes_past_the_uplink() -> None:
    rules = ruleset({}, [SplitRoute(PORT, SUBNET, ())])

    assert f"\tcounter direct_from_clients_{PORT} {{ }}" in rules
    assert (
        f'iifname "lg{PORT}" oifname != "lu{PORT}" counter name "direct_from_clients_{PORT}"'
        in rules
    )
    assert (
        f'iifname != "lu{PORT}" oifname "lg{PORT}" counter name "direct_to_clients_{PORT}"' in rules
    )
    assert "direct_" not in ruleset({PORT: A})


def test_the_state_of_a_split_group_has_its_direct_traffic(
    network: FakeNetwork, folder: Path
) -> None:
    agent = started(network, folder)
    network.bytes = {
        f"from_clients_{PORT}": 900,
        f"to_clients_{PORT}": 5000,
        f"direct_from_clients_{PORT}": 300,
        f"direct_to_clients_{PORT}": 1000,
    }

    agent.tick()

    state = split_state(folder)
    assert (state.bytes_from_clients, state.bytes_to_clients) == (900, 5000)
    assert (state.bytes_direct_from_clients, state.bytes_direct_to_clients) == (300, 1000)


def test_a_whitelist_group_marks_only_the_networks_of_its_list(
    network: FakeNetwork, folder: Path
) -> None:
    write_config(folder, moment=2, list_mode=ListMode.INCLUDE)

    started(network, folder)

    listed = f'iifname "lg{PORT}" ip daddr != @local ip daddr @ru_{PORT} ip daddr != @dl_{PORT}'
    assert listed in network.rules
    assert f"!= @ru_{PORT}" not in network.rules


def test_whitelist_ruleset_marks_the_list_the_abroad_domains_and_other_dns() -> None:
    mark = f"meta mark set {MARK_BASE + PORT}"
    route = SplitRoute(PORT, SUBNET, ("104.16.0.0/13",), ("ya.ru",), ("1.2.3.4",), DNS, "include")

    rules = ruleset({}, [route])

    assert f'iifname "lg{PORT}" ip daddr != @local ip daddr @da_{PORT} {mark}' in rules
    assert (
        f'iifname "lg{PORT}" ip daddr != @local ip daddr @ru_{PORT} ip daddr != @dl_{PORT} {mark}'
    ) in rules
    assert f'iifname "lg{PORT}" ip daddr != @local udp dport 53 {mark}' in rules
    assert f'iifname "lg{PORT}" ip daddr != @local tcp dport 53 {mark}' in rules
    assert "redirect to :53" in rules


def test_blacklist_ruleset_does_not_mark_dns_by_port() -> None:
    rules = ruleset({}, [SplitRoute(PORT, SUBNET, ("5.8.0.0/15",), dns=DNS)])

    assert "ip daddr != @local udp dport 53" not in rules


def test_split_ruleset_redirects_dns_only_with_addresses() -> None:
    redirect = f'iifname "lg{PORT}" ip daddr {{ 1.1.1.1, 1.0.0.1 }} udp dport 53 redirect to :53'

    assert redirect in ruleset({}, [SplitRoute(PORT, SUBNET, dns=DNS)])
    assert "dport 53" not in ruleset({}, [SplitRoute(PORT, SUBNET)])


def test_split_ruleset_with_an_empty_list_sends_everything_over_the_uplink() -> None:
    rules = ruleset({}, [SplitRoute(PORT, SUBNET)])

    assert "elements" not in rules.split("set local", 1)[1].split(f"set ru_{PORT}", 1)[1]


def test_forward_only_ruleset_has_no_sets() -> None:
    assert "set " not in ruleset({PORT: A})


NFT_KEYWORDS = frozenset(
    [
        "accept",
        "chain",
        "counter",
        "ct",
        "dnat",
        "drop",
        "elements",
        "filter",
        "flags",
        "hook",
        "ip",
        "map",
        "mark",
        "masquerade",
        "meta",
        "nat",
        "policy",
        "priority",
        "route",
        "rule",
        "set",
        "snat",
        "table",
        "tcp",
        "type",
        "udp",
    ]
)


def mixed_ruleset() -> str:
    split = SplitRoute(
        9733, SUBNET, ("5.8.0.0/15",), ("93.184.215.14",), ("5.8.1.1", "5.8.1.2"), DNS
    )
    whitelist = SplitRoute(9734, "10.8.2.0/24", ("104.16.0.0/13",), list_mode="include")
    return ruleset({PORT: A, 51820: None}, [split, whitelist])


def test_chain_and_set_names_are_not_nft_keywords() -> None:
    names = re.findall(r"^\t(?:chain|set) (\S+) \{", mixed_ruleset(), re.MULTILINE)

    assert names and not NFT_KEYWORDS.intersection(names)


def skip_without_nft(reason: str) -> NoReturn:
    """Skip the nft check, unless CI demands it (LAMNEZIA_REQUIRE_NFT=1) and a skip must fail."""
    if os.environ.get("LAMNEZIA_REQUIRE_NFT") == "1":
        pytest.fail(f"{reason}, but LAMNEZIA_REQUIRE_NFT=1")
    pytest.skip(reason)


def test_ruleset_passes_the_nft_syntax_check() -> None:
    nft = shutil.which("nft")
    if nft is None:
        skip_without_nft("nft is not installed")
    check = subprocess.run(
        [nft, "-c", "-f", "-"], input=mixed_ruleset(), capture_output=True, text=True, check=False
    )
    if "Operation not permitted" in check.stderr:
        skip_without_nft("nft needs CAP_NET_ADMIN")
    assert check.returncode == 0, check.stderr


def test_parse_networks_merges_and_skips_comments() -> None:
    assert parse_networks("5.9.0.0/16\n5.8.0.0/16 # x\n\n10.0.0.0/8\n") == (
        "5.8.0.0/15",
        "10.0.0.0/8",
    )


def test_settings_refuse_an_unknown_routing() -> None:
    text = RelayConfig((group(),)).to_json().replace('"routing": "split"', '"routing": "other"')

    with pytest.raises(ValueError, match="routing"):
        Settings.parse(text)


def test_settings_read_the_list_mode_of_a_split_group() -> None:
    blacklist = Settings.parse(RelayConfig((group(),)).to_json()).groups[0].split
    whitelist = Settings.parse(RelayConfig((group(list_mode=ListMode.INCLUDE),)).to_json())

    assert blacklist is not None and blacklist.list_mode == "exclude"
    assert whitelist.groups[0].split is not None
    assert whitelist.groups[0].split.list_mode == "include"


def test_settings_refuse_an_unknown_list_mode() -> None:
    text = RelayConfig((group(),)).to_json().replace('"list_mode": "exclude"', '"list_mode": "x"')

    with pytest.raises(ValueError, match="list mode"):
        Settings.parse(text)


def test_settings_without_a_list_mode_read_a_blacklist() -> None:
    text = RelayConfig((group(),)).to_json().replace(', "list_mode": "exclude"', "")

    split = Settings.parse(text).groups[0].split
    assert split is not None and split.list_mode == "exclude"


def test_settings_read_a_split_group() -> None:
    settings = Settings.parse(
        RelayConfig((group(),), uplink_check=IPv4Address("9.9.9.9")).to_json()
    )

    split = settings.groups[0].split
    assert split is not None and split.subnet == SUBNET
    assert split.dns == DNS
    assert settings.uplink_check == "9.9.9.9"
    assert settings.groups[0].mark == MARK_BASE + PORT
    assert settings.groups[0].backends[0] == Backend(A.host, A.port)
