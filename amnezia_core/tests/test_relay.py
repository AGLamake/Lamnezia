import json
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv4Interface
from typing import Any

import pytest

from amnezia_core.errors import RelayFormatError
from amnezia_core.keys import KeyPair
from amnezia_core.relay import Backend, RelayConfig, RelayGroup, RelayState, ServicePeer

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
        ],
    )
    def test_rejects_a_spoiled_file(self, spoil: Any) -> None:
        data = json.loads(json.dumps(STATE))
        spoil(data)

        with pytest.raises(RelayFormatError):
            RelayState.parse(json.dumps(data))
