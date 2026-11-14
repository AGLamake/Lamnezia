import json
from collections.abc import Callable
from typing import Any

import pytest

from amnezia_core.awg import AWG_CLIENTS_TABLE_PATH, AWG_CONFIG_PATH, AWG_PSK_PATH, AwgServerConfig
from amnezia_core.awg_backup import AwgServerFiles
from amnezia_core.awg_export import has_awg3_params
from amnezia_core.awg_group import (
    AwgGroupIdentity,
    Divergence,
    Mismatch,
    has_clients,
    standalone_files,
)
from amnezia_core.errors import ConfigFormatError, InvalidKeyError
from amnezia_core.keys import KeyPair
from amnezia_core.tests.conftest import FIRST_PEER, SECOND_PEER

PSK = "u+xLktHw90auwUVtjZEJVpNCbfbFK8GNmGegY/Z0XYs="

Edit = Callable[[AwgServerConfig], object]
Clients = list[dict[str, Any]]


@pytest.fixture
def leader(read_fixture: Callable[[str], str]) -> AwgServerFiles:
    return AwgServerFiles(read_fixture("awg0.conf"), read_fixture("clientsTable.json"), PSK)


@pytest.fixture
def identity(leader: AwgServerFiles) -> AwgGroupIdentity:
    return AwgGroupIdentity(leader)


def member_with(leader: AwgServerFiles, edit: Edit) -> AwgServerFiles:
    config = AwgServerConfig.parse(leader.config)
    edit(config)
    return AwgServerFiles(config.serialize(), leader.clients_table, leader.preshared_key)


def clients_with(leader: AwgServerFiles, edit: Callable[[Clients], None]) -> AwgServerFiles:
    clients = json.loads(leader.clients_table)
    edit(clients)
    return AwgServerFiles(leader.config, json.dumps(clients), leader.preshared_key)


class TestClone:
    def test_member_files_are_the_leaders_byte_for_byte(
        self, identity: AwgGroupIdentity, leader: AwgServerFiles
    ) -> None:
        files = identity.member_files()

        assert files == leader.by_path()
        assert files[AWG_CONFIG_PATH] == leader.config.encode()
        assert files[AWG_CLIENTS_TABLE_PATH] == leader.clients_table.encode()
        assert files[AWG_PSK_PATH] == f"{PSK}\n".encode()

    def test_clone_matches_the_group(self, identity: AwgGroupIdentity) -> None:
        files = identity.member_files()
        clone = AwgServerFiles(
            files[AWG_CONFIG_PATH].decode(),
            files[AWG_CLIENTS_TABLE_PATH].decode(),
            files[AWG_PSK_PATH].decode(),
        )

        assert identity.compare(clone) == []

    def test_statistics_the_app_writes_per_server_do_not_count(
        self, identity: AwgGroupIdentity, leader: AwgServerFiles
    ) -> None:
        def own_stats(clients: Clients) -> None:
            clients[1]["userData"] |= {
                "latestHandshake": "5s ago",
                "dataReceived": "3 KiB",
                "dataSent": "9 KiB",
                "allowed_ips": "10.8.1.2/32",
            }

        assert identity.compare(clients_with(leader, own_stats)) == []

    def test_peer_order_does_not_count(
        self, identity: AwgGroupIdentity, leader: AwgServerFiles
    ) -> None:
        def reorder(config: AwgServerConfig) -> None:
            first = config.find_peer(FIRST_PEER)
            assert first is not None
            config.remove_peer(FIRST_PEER)
            config.add_peer(first)

        assert identity.compare(member_with(leader, reorder)) == []


class TestDivergences:
    def test_server_key(self, identity: AwgGroupIdentity, leader: AwgServerFiles) -> None:
        def other_key(config: AwgServerConfig) -> None:
            config.interface.set("PrivateKey", KeyPair.generate().private_key)

        assert identity.compare(member_with(leader, other_key)) == [
            Divergence(Mismatch.PRIVATE_KEY)
        ]

    def test_preshared_key(self, identity: AwgGroupIdentity, leader: AwgServerFiles) -> None:
        member = AwgServerFiles(leader.config, leader.clients_table, f"{SECOND_PEER}\n")

        assert identity.compare(member) == [Divergence(Mismatch.PRESHARED_KEY)]

    @pytest.mark.parametrize(
        ("name", "value"),
        [("Jc", "5"), ("H1", "1-2"), ("ListenPort", "51820"), ("Address", "10.9.0.0/24")],
    )
    def test_interface_entry(
        self, identity: AwgGroupIdentity, leader: AwgServerFiles, name: str, value: str
    ) -> None:
        member = member_with(leader, lambda config: config.interface.set(name, value))

        assert identity.compare(member) == [Divergence(Mismatch.INTERFACE, name)]

    def test_special_junk_kept_in_a_comment(
        self, identity: AwgGroupIdentity, leader: AwgServerFiles
    ) -> None:
        def other_i1(config: AwgServerConfig) -> None:
            config.update_interface(55424, {**config.awg_params, "I1": "<r 4>"})

        assert identity.compare(member_with(leader, other_i1)) == [
            Divergence(Mismatch.INTERFACE, "I1")
        ]

    def test_entry_only_the_member_has(
        self, identity: AwgGroupIdentity, leader: AwgServerFiles
    ) -> None:
        member = member_with(leader, lambda config: config.interface.set("S1", "1"))
        extra = member_with(member, lambda config: config.interface.set("KeepaliveTimeout", "5"))

        assert identity.compare(extra) == [
            Divergence(Mismatch.INTERFACE, "S1"),
            Divergence(Mismatch.INTERFACE, "KeepaliveTimeout"),
        ]

    def test_peers(self, identity: AwgGroupIdentity, leader: AwgServerFiles) -> None:
        stranger = KeyPair.generate().public_key

        def drift(config: AwgServerConfig) -> None:
            config.remove_peer(FIRST_PEER)
            second = config.find_peer(SECOND_PEER)
            assert second is not None
            config.remove_peer(SECOND_PEER)
            config.add_peer(type(second)(SECOND_PEER, ("10.8.1.9/32",), second.preshared_key))
            config.add_peer(type(second)(stranger, ("10.8.1.3/32",), PSK))

        assert identity.compare(member_with(leader, drift)) == [
            Divergence(Mismatch.PEER_MISSING, FIRST_PEER),
            Divergence(Mismatch.PEER_EXTRA, stranger),
            Divergence(Mismatch.PEER_CHANGED, SECOND_PEER),
        ]

    def test_clients(self, identity: AwgGroupIdentity, leader: AwgServerFiles) -> None:
        def drift(clients: Clients) -> None:
            clients[1]["userData"]["clientName"] = "Renamed"
            clients[0]["clientId"] = "other"

        assert identity.compare(clients_with(leader, drift)) == [
            Divergence(Mismatch.CLIENT_MISSING, FIRST_PEER),
            Divergence(Mismatch.CLIENT_EXTRA, "other"),
            Divergence(Mismatch.CLIENT_CHANGED, SECOND_PEER),
        ]

    def test_key_disabled_only_on_the_member(
        self, identity: AwgGroupIdentity, leader: AwgServerFiles
    ) -> None:
        member = member_with(leader, lambda config: config.remove_peer(FIRST_PEER))

        assert identity.compare(member) == [Divergence(Mismatch.PEER_MISSING, FIRST_PEER)]


class TestLeader:
    def test_port_and_subnet_of_the_group(self, identity: AwgGroupIdentity) -> None:
        assert (identity.port, str(identity.subnet)) == (55424, "10.8.1.0/24")

    def test_needs_listen_port(self, leader: AwgServerFiles) -> None:
        broken = member_with(leader, lambda config: config.interface.remove("ListenPort"))

        with pytest.raises(ConfigFormatError):
            AwgGroupIdentity(broken)

    def test_needs_a_valid_preshared_key(self, leader: AwgServerFiles) -> None:
        with pytest.raises(InvalidKeyError):
            AwgGroupIdentity(AwgServerFiles(leader.config, leader.clients_table, "nope"))


def test_standalone_files_keep_the_address_and_port_with_a_new_identity(
    leader: AwgServerFiles,
) -> None:
    old = AwgServerConfig.parse(leader.config)

    fresh = standalone_files(old.address, 55424)

    config = AwgServerConfig.parse(fresh.config)
    assert config.address == old.address
    assert config.listen_port == 55424
    assert config.private_key not in {None, old.private_key}
    assert config.peers == []
    assert has_awg3_params(config.awg_params)
    assert fresh.preshared_key != leader.preshared_key
    assert not has_clients(fresh)


def test_server_with_peers_or_clients_has_clients(leader: AwgServerFiles) -> None:
    fresh = standalone_files(AwgServerConfig.parse(leader.config).address, 55424)
    only_clients = AwgServerFiles(fresh.config, leader.clients_table, PSK)

    assert has_clients(leader)
    assert has_clients(only_clients)
