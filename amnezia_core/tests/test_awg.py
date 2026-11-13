from collections.abc import Callable
from ipaddress import IPv4Address, IPv4Interface, IPv4Network

import pytest

from amnezia_core.awg import AwgServerConfig, Peer, Renumbering
from amnezia_core.errors import ConfigFormatError
from amnezia_core.tests.conftest import FIRST_PEER, SECOND_PEER

NEW_PEER = Peer("bmV3LXBlZXI=", ("10.8.1.3/32",), "cHNr")


@pytest.fixture
def config(read_fixture: Callable[[str], str]) -> AwgServerConfig:
    return AwgServerConfig.parse(read_fixture("awg0.conf"))


def test_round_trip_keeps_upstream_file(read_fixture: Callable[[str], str]) -> None:
    text = read_fixture("awg0.conf")

    assert AwgServerConfig.parse(text).serialize() == text


def test_reads_interface(config: AwgServerConfig) -> None:
    assert config.private_key == "+I1o14tu7eCcREXj1rrqH+CzICpdRvfQoTHi5vo+v0s="
    assert config.listen_port == 55424
    assert config.address == IPv4Interface("10.8.1.0/24")


def test_reads_present_awg_params_only(config: AwgServerConfig) -> None:
    params = config.awg_params

    assert params["Jc"] == "4"
    assert params["H1"] == "1020325451-1025713284"
    # Upstream keeps I1 commented out (the app reads it from there) and deletes empty parameters.
    assert params["I1"].startswith("<r 2><b 0x8580")
    assert "I2" not in params
    assert "HeaderProtectionKey" not in params


def test_reads_peers(config: AwgServerConfig) -> None:
    first, second = config.peers

    assert first.public_key == FIRST_PEER
    assert first.preshared_key == "u+xLktHw90auwUVtjZEJVpNCbfbFK8GNmGegY/Z0XYs="
    assert first.addresses == [IPv4Address("10.8.1.1")]
    assert second.public_key == SECOND_PEER


def test_add_peer_appends_like_the_app(
    config: AwgServerConfig, read_fixture: Callable[[str], str]
) -> None:
    config.add_peer(NEW_PEER)

    expected = "[Peer]\nPublicKey = bmV3LXBlZXI=\nPresharedKey = cHNr\nAllowedIPs = 10.8.1.3/32\n\n"
    assert config.serialize() == read_fixture("awg0.conf") + expected


def test_add_peer_rejects_duplicate(config: AwgServerConfig) -> None:
    with pytest.raises(ConfigFormatError):
        config.add_peer(Peer(FIRST_PEER, ("10.8.1.9/32",)))


def test_remove_peer_drops_its_section(
    config: AwgServerConfig, read_fixture: Callable[[str], str]
) -> None:
    second_section = read_fixture("awg0.conf").split("[Peer]")[2]

    assert config.remove_peer(FIRST_PEER) is True
    assert config.find_peer(FIRST_PEER) is None
    assert config.serialize().endswith("[Peer]" + second_section)
    assert config.remove_peer(FIRST_PEER) is False


def test_peer_without_psk_and_with_several_allowed_ips() -> None:
    config = AwgServerConfig.parse(
        "[Interface]\nAddress = fd00::1/64, 10.0.0.1/16\n"
        "[Peer]\nPublicKey = k\nAllowedIPs = 10.0.0.5/32, fd00::5/128\n"
        "AllowedIPs = 192.168.0.0/24\n"
    )
    peer = config.peers[0]

    assert peer.preshared_key is None
    assert peer.allowed_ips == ("10.0.0.5/32", "fd00::5/128", "192.168.0.0/24")
    assert peer.addresses == [IPv4Address("10.0.0.5")]
    assert config.address == IPv4Interface("10.0.0.1/16")
    assert Peer("k", ("10.0.0.5/32",)).to_section().lines == [
        "[Peer]",
        "PublicKey = k",
        "AllowedIPs = 10.0.0.5/32",
        "",
    ]


@pytest.mark.parametrize(
    "text",
    ["[Peer]\nPublicKey = k\n", "[Interface]\n[Interface]\n"],
)
def test_requires_single_interface(text: str) -> None:
    with pytest.raises(ConfigFormatError):
        AwgServerConfig.parse(text)


def test_missing_values() -> None:
    config = AwgServerConfig.parse("[Interface]\n[Peer]\nAllowedIPs = 10.0.0.2/32\n")

    assert config.listen_port is None
    assert config.private_key is None
    with pytest.raises(ConfigFormatError):
        _ = config.address
    with pytest.raises(ConfigFormatError):
        _ = config.peers


def test_update_interface_changes_values_in_place(config: AwgServerConfig) -> None:
    params = {**config.awg_params, "Jc": "8", "H1": "5-10", "I1": "<r 4>", "I2": "<b 0x01>"}

    config.update_interface(40000, params)

    assert config.listen_port == 40000
    assert config.awg_params == params
    text = config.serialize()
    assert "Jc = 8\nJmin = 10\n" in text
    assert "# I1 = <r 4>\n# I2 = <b 0x01>\n[Peer]" in text
    assert len(config.peers) == 2


def test_update_interface_drops_empty_parameters(config: AwgServerConfig) -> None:
    config.update_interface(55424, {"Jc": "4", "RandomTrailers": "on"})

    assert config.awg_params == {"Jc": "4", "RandomTrailers": "on"}
    assert "# I1 =" not in config.serialize()


def test_update_interface_refuses_a_line_break(config: AwgServerConfig) -> None:
    with pytest.raises(ConfigFormatError):
        config.update_interface(55424, {"I1": "<r 2>\n[Peer]"})


def test_renumber_keeps_the_host_part_of_every_address(config: AwgServerConfig) -> None:
    config.renumber(Renumbering(IPv4Network("10.8.1.0/24"), IPv4Network("10.9.0.0/23")))

    assert config.address == IPv4Interface("10.9.0.0/23")
    assert [peer.allowed_ips for peer in config.peers] == [("10.9.0.1/32",), ("10.9.0.2/32",)]
    assert "Address = 10.9.0.0/23\n" in config.serialize()


def test_renumber_leaves_routes_outside_the_subnet() -> None:
    config = AwgServerConfig.parse(
        "[Interface]\nAddress = 10.8.1.0/24, fd00::1/64\n\n"
        "[Peer]\nPublicKey = a2V5\nAllowedIPs = 10.8.1.7/32\nAllowedIPs = 192.168.5.0/24\n"
    )

    config.renumber(Renumbering(IPv4Network("10.8.1.0/24"), IPv4Network("172.16.4.0/24")))

    assert config.interface.get("Address") == "172.16.4.0/24, fd00::1/64"
    assert config.peers[0].allowed_ips == ("172.16.4.7/32", "192.168.5.0/24")


def test_renumber_refuses_a_smaller_subnet() -> None:
    with pytest.raises(ConfigFormatError):
        Renumbering(IPv4Network("10.8.1.0/24"), IPv4Network("10.9.0.0/25"))
