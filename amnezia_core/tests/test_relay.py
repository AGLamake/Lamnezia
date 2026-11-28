import json
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from typing import Any

import pytest

from amnezia_core.awg import Peer
from amnezia_core.errors import InvalidKeyError, RelayFormatError
from amnezia_core.keys import KeyPair
from amnezia_core.relay import (
    Backend,
    Clone,
    ClonePeers,
    NetworkList,
    RelayConfig,
    RelayGroup,
    RelayState,
    ResolverState,
    ResolverStatus,
    Routing,
    ServicePeer,
    SplitRouting,
    SwitchOutcome,
    SwitchRequest,
    SwitchResult,
    UplinkState,
    clone_peers_file,
    networks_file,
)

SERVICE_KEY = KeyPair.generate()
SERVER_KEY = KeyPair.generate().public_key
PSK = "u+xLktHw90auwUVtjZEJVpNCbfbFK8GNmGegY/Z0XYs="
A = Backend(IPv4Address("203.0.113.10"), 55424)
B = Backend(IPv4Address("198.51.100.20"), 55424)


def service() -> ServicePeer:
    return ServicePeer(
        private_key=SERVICE_KEY.private_key,
        address=IPv4Interface("10.8.1.254/32"),
        server_public_key=SERVER_KEY,
        preshared_key=PSK,
        awg_params={"Jc": "4", "H1": "1020325451-1025713284", "I1": "<r 2>"},
    )


def config() -> RelayConfig:
    return RelayConfig((RelayGroup(55424, (A, B), service()),))


def config_data() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(config().to_json())
    return data


class TestRelayConfig:
    def test_defaults_from_the_research(self) -> None:
        assert (config().check_interval, config().failures) == (5, 3)

    def test_round_trip(self) -> None:
        assert RelayConfig.from_json(config().to_json()) == config()

    def test_file_layout(self) -> None:
        data = config_data()

        assert data["version"] == 1
        assert data["groups"][0]["port"] == 55424
        assert data["groups"][0]["backends"] == [
            {"host": "203.0.113.10", "port": 55424},
            {"host": "198.51.100.20", "port": 55424},
        ]
        assert data["groups"][0]["service"]["address"] == "10.8.1.254/32"

    def test_repr_hides_the_secrets(self) -> None:
        text = repr(config())

        assert SERVICE_KEY.private_key not in text
        assert PSK not in text

    def test_groups_need_their_own_ports(self) -> None:
        group = RelayGroup(55424, (A,), service())

        with pytest.raises(RelayFormatError):
            RelayConfig((group, group))

    @pytest.mark.parametrize("backends", [(), (A, A)])
    def test_group_needs_distinct_backends(self, backends: tuple[Backend, ...]) -> None:
        with pytest.raises(RelayFormatError):
            RelayGroup(55424, backends, service())

    @pytest.mark.parametrize("port", [0, 65536])
    def test_port_range(self, port: int) -> None:
        with pytest.raises(RelayFormatError):
            Backend(IPv4Address("203.0.113.10"), port)

    @pytest.mark.parametrize(("check_interval", "failures"), [(0, 3), (5, 0)])
    def test_probing_needs_positive_numbers(self, check_interval: int, failures: int) -> None:
        with pytest.raises(RelayFormatError):
            RelayConfig((), check_interval, failures)

    @pytest.mark.parametrize(
        "spoil",
        [
            lambda data: data.update(version=2),
            lambda data: data["groups"][0]["backends"][0].update(host="vpn.example.com"),
            lambda data: data["groups"][0].update(port="55424"),
            lambda data: data["groups"][0].update(port=True),
            lambda data: data["groups"][0]["service"].update(private_key="nope"),
            lambda data: data["groups"][0]["service"]["awg_params"].update(Jc=4),
            lambda data: data.pop("failures"),
        ],
    )
    def test_rejects_a_spoiled_file(self, spoil: Any) -> None:
        data = config_data()
        spoil(data)

        with pytest.raises(RelayFormatError):
            RelayConfig.from_json(json.dumps(data))

    def test_rejects_non_json(self) -> None:
        with pytest.raises(RelayFormatError):
            RelayConfig.from_json("groups: []")


STATE = {
    "version": 1,
    "updated_at": 1731580000,
    "groups": [
        {
            "port": 55424,
            "active": {"host": "198.51.100.20", "port": 55424},
            "switched_at": 1731579900,
            "bytes_from_clients": 1200,
            "bytes_to_clients": 34000,
            "backends": [
                {
                    "host": "203.0.113.10",
                    "port": 55424,
                    "alive": False,
                    "failures": 7,
                    "checked_at": 1731579995,
                },
                {
                    "host": "198.51.100.20",
                    "port": 55424,
                    "alive": True,
                    "failures": 0,
                    "checked_at": None,
                },
            ],
        }
    ],
}


class TestRelayState:
    def test_parses_the_agent_file(self) -> None:
        state = RelayState.parse(json.dumps(STATE))
        group = state.group(55424)

        assert state.updated_at == datetime(2024, 11, 14, 10, 26, 40, tzinfo=UTC)
        assert group is not None
        assert group.active == B
        assert group.switched_at == datetime(2024, 11, 14, 10, 25, tzinfo=UTC)
        assert (group.bytes_from_clients, group.bytes_to_clients) == (1200, 34000)
        assert [(item.backend, item.alive, item.failures) for item in group.backends] == [
            (A, False, 7),
            (B, True, 0),
        ]
        assert group.backends[1].checked_at is None

    def test_no_active_backend_yet(self) -> None:
        data = json.loads(json.dumps(STATE))
        data["groups"][0].update(active=None, switched_at=None)

        group = RelayState.parse(json.dumps(data)).group(55424)

        assert group is not None
        assert (group.active, group.switched_at) == (None, None)

    def test_unknown_port(self) -> None:
        assert RelayState.parse(json.dumps(STATE)).group(1) is None

    @pytest.mark.parametrize(
        "spoil",
        [
            lambda data: data.pop("updated_at"),
            lambda data: data["groups"][0]["backends"][0].update(alive="yes"),
            lambda data: data["groups"][0].update(active="198.51.100.20:55424"),
            lambda data: data["groups"][0].update(bytes_to_clients=-1),
            lambda data: data.update(groups={}),
            lambda data: data.update(switch="r1"),
            lambda data: data.update(switch={"id": "r1", "outcome": "maybe"}),
            lambda data: data.update(switch={"outcome": "switched"}),
            lambda data: data.update(resolver="running"),
            lambda data: data.update(resolver={"status": "sleeping"}),
            lambda data: data.update(resolver={"status": "failed", "error": 1}),
            lambda data: data.update(resolver={"status": "running", "lookup_error": 1}),
        ],
    )
    def test_rejects_a_spoiled_file(self, spoil: Any) -> None:
        data = json.loads(json.dumps(STATE))
        spoil(data)

        with pytest.raises(RelayFormatError):
            RelayState.parse(json.dumps(data))

    def test_an_older_agent_gives_no_switch_answer(self) -> None:
        assert RelayState.parse(json.dumps(STATE)).switch is None

    def test_reads_the_switch_answer(self) -> None:
        data = {**STATE, "switch": {"id": "r1", "outcome": "unreachable"}}

        state = RelayState.parse(json.dumps(data))

        assert state.switch == SwitchResult("r1", SwitchOutcome.UNREACHABLE)

    def test_an_older_agent_tells_no_resolver(self) -> None:
        assert RelayState.parse(json.dumps(STATE)).resolver == ResolverState()
        assert ResolverState().status is ResolverStatus.UNKNOWN

    @pytest.mark.parametrize(
        ("resolver", "expected"),
        [
            ({"status": "running"}, ResolverState(ResolverStatus.RUNNING)),
            ({"status": "off"}, ResolverState(ResolverStatus.OFF)),
            ({"status": "missing"}, ResolverState(ResolverStatus.MISSING)),
            (
                {"status": "failed", "error": "resolver stopped with code 1"},
                ResolverState(ResolverStatus.FAILED, "resolver stopped with code 1"),
            ),
            (
                {"status": "running", "lookup_error": "2 of 3 lookups failed: timed out"},
                ResolverState(
                    ResolverStatus.RUNNING, lookup_error="2 of 3 lookups failed: timed out"
                ),
            ),
        ],
    )
    def test_reads_the_resolver_status(
        self, resolver: dict[str, str], expected: ResolverState
    ) -> None:
        state = RelayState.parse(json.dumps({**STATE, "resolver": resolver}))

        assert state.resolver == expected

    def test_the_resolver_status_goes_back_and_forth(self) -> None:
        resolver = ResolverState(ResolverStatus.FAILED, "bad config", "1 of 1 lookups failed")

        assert ResolverState.from_dict(resolver.to_dict()) == resolver


class TestSwitchRequest:
    def test_file_layout(self) -> None:
        text = SwitchRequest("r1", 55424, B).to_json()

        assert json.loads(text) == {
            "version": 1,
            "id": "r1",
            "port": 55424,
            "backend": {"host": "198.51.100.20", "port": 55424},
        }

    def test_port_range(self) -> None:
        with pytest.raises(RelayFormatError):
            SwitchRequest("r1", 0, B)


CLONE_KEY = KeyPair.generate()


def split_group() -> RelayGroup:
    uplink = ServicePeer(
        private_key=KeyPair.generate().private_key,
        address=IPv4Interface("10.8.1.253/32"),
        server_public_key=SERVER_KEY,
        preshared_key=PSK,
        awg_params={"Jc": "6"},
    )
    clone = Clone(CLONE_KEY.private_key, IPv4Interface("10.8.1.0/24"), {"Jc": "6", "S1": "12"})
    dns = (IPv4Address("1.1.1.1"), IPv4Address("1.0.0.1"))
    split = SplitRouting(clone, uplink, dns=dns)
    return RelayGroup(55424, (A, B), service(), Routing.SPLIT, split)


class TestSplitRouting:
    def test_round_trip(self) -> None:
        config = RelayConfig((split_group(),), recovery=4, uplink_check=IPv4Address("9.9.9.9"))

        assert RelayConfig.from_json(config.to_json()) == config

    def test_file_layout(self) -> None:
        data = json.loads(RelayConfig((split_group(),)).to_json())
        group = data["groups"][0]

        assert (data["recovery"], data["uplink_check"]) == (6, "1.1.1.1")
        assert group["routing"] == "split"
        assert group["split"]["kind"] == "awg"
        assert group["split"]["clone"]["address"] == "10.8.1.0/24"
        assert group["split"]["uplink"]["address"] == "10.8.1.253/32"
        assert group["split"]["dns"] == ["1.1.1.1", "1.0.0.1"]

    def test_a_split_group_of_an_older_panel_has_no_dns(self) -> None:
        data = json.loads(RelayConfig((split_group(),)).to_json())
        data["groups"][0]["split"].pop("dns")

        split = RelayConfig.from_json(json.dumps(data)).groups[0].split

        assert split is not None and split.dns == ()

    def test_a_config_of_an_older_panel_forwards(self) -> None:
        data = config_data()
        for key in ("recovery", "uplink_check"):
            data.pop(key)
        data["groups"][0].pop("routing")

        parsed = RelayConfig.from_json(json.dumps(data))

        assert parsed.groups[0].routing is Routing.FORWARD
        assert (parsed.recovery, parsed.uplink_check) == (6, IPv4Address("1.1.1.1"))

    def test_split_group_needs_its_clone_and_uplink(self) -> None:
        with pytest.raises(RelayFormatError):
            RelayGroup(55424, (A,), service(), Routing.SPLIT)

    def test_repr_hides_the_clone_key(self) -> None:
        assert CLONE_KEY.private_key not in repr(split_group())

    def test_clone_address_is_not_the_broadcast(self) -> None:
        with pytest.raises(RelayFormatError):
            Clone(CLONE_KEY.private_key, IPv4Interface("10.8.1.255/24"), {})

    def test_clone_of_a_server_drops_special_junk_and_empty_params(self) -> None:
        params = {"Jc": "4", "S1": "", "H1": "1020325451-1025713284", "I1": "<r 2>", "X": "1"}

        clone = Clone.of_server(CLONE_KEY.private_key, "10.8.1.0/24", params)

        assert clone.awg_params == {"Jc": "4", "H1": "1020325451-1025713284"}
        assert str(clone.address) == "10.8.1.0/24"

    def test_clone_takes_the_server_address_of_amnezia(self) -> None:
        clone = Clone(CLONE_KEY.private_key, IPv4Interface("10.8.1.0/24"), {})

        assert str(clone.address) == "10.8.1.0/24"

    def test_recovery_must_be_positive(self) -> None:
        with pytest.raises(RelayFormatError):
            RelayConfig((), recovery=0)

    @pytest.mark.parametrize(
        "spoil",
        [
            lambda group: group.update(routing="tunnel"),
            lambda group: group.update(split="yes"),
            lambda group: group["split"].update(kind="ssh"),
            lambda group: group["split"]["clone"].update(address="10.8.1.1/33"),
            lambda group: group["split"]["clone"].update(private_key="nope"),
            lambda group: group["split"].pop("uplink"),
            lambda group: group["split"].update(dns="1.1.1.1"),
            lambda group: group["split"].update(dns=["one.one"]),
        ],
    )
    def test_rejects_a_spoiled_group(self, spoil: Any) -> None:
        data = json.loads(RelayConfig((split_group(),)).to_json())
        spoil(data["groups"][0])

        with pytest.raises(RelayFormatError):
            RelayConfig.from_json(json.dumps(data))

    def test_rejects_a_bad_uplink_check(self) -> None:
        data = {**config_data(), "uplink_check": "one.one"}

        with pytest.raises(RelayFormatError):
            RelayConfig.from_json(json.dumps(data))

    def test_file_names(self) -> None:
        assert (networks_file(55424), clone_peers_file(55424)) == (
            "networks-55424.txt",
            "peers-55424.conf",
        )


class TestClonePeers:
    def test_sections_as_amnezia_writes_them(self) -> None:
        first, second = KeyPair.generate().public_key, KeyPair.generate().public_key
        peers = ClonePeers((Peer(first, ("10.8.1.1/32",), PSK), Peer(second, ("10.8.1.2/32",))))

        assert peers.to_text() == (
            f"[Peer]\nPublicKey = {first}\nPresharedKey = {PSK}\nAllowedIPs = 10.8.1.1/32\n\n"
            f"[Peer]\nPublicKey = {second}\nAllowedIPs = 10.8.1.2/32\n\n"
        )

    def test_no_peers_is_an_empty_file(self) -> None:
        assert ClonePeers(()).to_text() == ""

    def test_rejects_a_peer_listed_twice(self) -> None:
        key = KeyPair.generate().public_key

        with pytest.raises(RelayFormatError):
            ClonePeers((Peer(key, ("10.8.1.1/32",)), Peer(key, ("10.8.1.2/32",))))

    def test_rejects_a_broken_key(self) -> None:
        with pytest.raises(InvalidKeyError):
            ClonePeers((Peer("nope", ("10.8.1.1/32",)),))


class TestNetworkList:
    def test_merges_and_sorts(self) -> None:
        networks = [IPv4Network(text) for text in ("5.8.0.0/17", "2.60.0.0/16", "5.8.128.0/17")]

        assert NetworkList.of(networks).to_text() == "2.60.0.0/16\n5.8.0.0/16\n"

    def test_parse_skips_comments_and_blank_lines(self) -> None:
        text = "# RU, 2026-11-19\n\n2.60.0.0/16\n5.8.0.0/16  # RIPE\n"

        assert NetworkList.parse(text).networks == (
            IPv4Network("2.60.0.0/16"),
            IPv4Network("5.8.0.0/16"),
        )

    @pytest.mark.parametrize(
        "text", ["", "# nothing\n", "2.60.0.0/16\nexample.com\n", "2.60.0.1/16\n"]
    )
    def test_refuses_an_empty_or_broken_file(self, text: str) -> None:
        with pytest.raises(RelayFormatError):
            NetworkList.parse(text)


class TestSplitState:
    def test_an_older_agent_tells_no_split(self) -> None:
        group = RelayState.parse(json.dumps(STATE)).group(55424)

        assert group is not None
        assert (group.routing, group.mode, group.uplink, group.networks) == (
            Routing.FORWARD,
            Routing.FORWARD,
            None,
            0,
        )
        assert not group.emergency

    def test_reads_the_emergency_forward(self) -> None:
        data = json.loads(json.dumps(STATE))
        uplink = {"endpoint": A.to_dict(), "alive": False, "checked_at": 1731579995}
        data["groups"][0].update(
            routing="split",
            mode="forward",
            mode_changed_at=1731579950,
            networks=8000,
            uplink=uplink,
        )

        group = RelayState.parse(json.dumps(data)).group(55424)

        assert group is not None
        assert group.emergency
        assert group.networks == 8000
        assert group.mode_changed_at == datetime(2024, 11, 14, 10, 25, 50, tzinfo=UTC)
        assert group.uplink == UplinkState(A, False, datetime(2024, 11, 14, 10, 26, 35, tzinfo=UTC))

    @pytest.mark.parametrize(
        "spoil",
        [
            lambda group: group.update(mode="tunnel"),
            lambda group: group.update(uplink="up"),
            lambda group: group.update(uplink={"endpoint": None, "alive": "yes"}),
            lambda group: group.update(networks=-1),
        ],
    )
    def test_rejects_a_spoiled_group(self, spoil: Any) -> None:
        data = json.loads(json.dumps(STATE))
        spoil(data["groups"][0])

        with pytest.raises(RelayFormatError):
            RelayState.parse(json.dumps(data))
