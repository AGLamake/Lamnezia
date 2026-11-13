"""Routes of speed-limited XRay clients in server.json."""

import json
from collections.abc import Callable
from typing import Any

import pytest

from amnezia_core.xray import XrayServerConfig

EMAIL = "a" * 64
OTHER = "b" * 64


@pytest.fixture
def server_text(read_fixture: Callable[[str], str]) -> str:
    return read_fixture("xray_server.json")


def routed(config: XrayServerConfig) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(config.serialize())
    return data


def marked(mark: int) -> dict[str, Any]:
    return {
        "tag": f"lamnezia-{mark}",
        "protocol": "freedom",
        "streamSettings": {"sockopt": {"mark": mark}},
    }


def test_each_client_gets_an_outbound_with_its_mark(server_text: str) -> None:
    config = XrayServerConfig.parse(server_text)

    config.set_speed_marks({OTHER: 1002, EMAIL: 1001})

    data = routed(config)
    assert data["outbounds"] == [{"protocol": "freedom"}, marked(1001), marked(1002)]
    assert data["routing"] == {
        "rules": [
            {"type": "field", "user": [EMAIL], "outboundTag": "lamnezia-1001"},
            {"type": "field", "user": [OTHER], "outboundTag": "lamnezia-1002"},
        ]
    }
    assert data["inbounds"] == json.loads(server_text)["inbounds"]


def test_routes_are_replaced_and_taken_out_to_the_file_of_the_app(server_text: str) -> None:
    config = XrayServerConfig.parse(server_text)
    config.set_speed_marks({EMAIL: 1001, OTHER: 1002})

    config.set_speed_marks({OTHER: 1001})
    assert routed(config)["outbounds"] == [{"protocol": "freedom"}, marked(1001)]
    assert routed(config)["routing"]["rules"][0]["user"] == [OTHER]

    config.set_speed_marks({})
    assert config.serialize() == server_text


def test_same_marks_leave_the_file_unchanged(server_text: str) -> None:
    config = XrayServerConfig.parse(server_text)
    config.set_speed_marks({EMAIL: 1001})
    text = config.serialize()

    config.set_speed_marks({EMAIL: 1001})

    assert config.serialize() == text


def test_rules_of_others_decide_first_and_stay(server_text: str) -> None:
    data = json.loads(server_text)
    block = {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"}
    data["outbounds"].append({"tag": "block", "protocol": "blackhole"})
    data["routing"] = {"domainStrategy": "AsIs", "rules": [block]}
    config = XrayServerConfig(data)

    config.set_speed_marks({EMAIL: 1001})
    assert routed(config)["routing"]["rules"][0] == block
    assert routed(config)["outbounds"][1]["tag"] == "block"

    config.set_speed_marks({})
    assert routed(config)["routing"] == {"domainStrategy": "AsIs", "rules": [block]}


def test_config_without_outbounds_keeps_a_default_one_first(server_text: str) -> None:
    data = json.loads(server_text)
    del data["outbounds"]
    config = XrayServerConfig(data)

    config.set_speed_marks({EMAIL: 1001})

    assert routed(config)["outbounds"] == [{"protocol": "freedom"}, marked(1001)]
