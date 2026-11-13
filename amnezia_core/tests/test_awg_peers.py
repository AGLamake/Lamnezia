from collections.abc import Callable
from datetime import datetime
from ipaddress import IPv4Address

import pytest

from amnezia_core.awg import Peer
from amnezia_core.awg_peers import AwgPeerRegistry
from amnezia_core.clients_table import ClientRecord, ClientsTable
from amnezia_core.errors import PeerConflictError
from amnezia_core.tests.conftest import FIRST_PEER, SECOND_PEER

CREATED = datetime(2026, 11, 2, 9, 5, 7)
PHONE = ClientRecord("Phone", CREATED)
NEW_KEY = "bmV3LXBlZXI="
PSK = "u+xLktHw90auwUVtjZEJVpNCbfbFK8GNmGegY/Z0XYs="


@pytest.fixture
def registry(read_fixture: Callable[[str], str]) -> AwgPeerRegistry:
    return AwgPeerRegistry.parse(read_fixture("awg0.conf"), read_fixture("clientsTable.json"))


def texts(registry: AwgPeerRegistry) -> tuple[str, str]:
    return registry.config.serialize(), registry.clients.serialize()


def test_new_client_is_appended_like_the_app_does(registry: AwgPeerRegistry) -> None:
    peer = registry.add_client(NEW_KEY, ClientRecord("Phone", CREATED, 7), preshared_key=PSK)

    assert peer == Peer(NEW_KEY, ("10.8.1.3/32",), PSK)
    assert registry.config.serialize().endswith(
        f"[Peer]\nPublicKey = {NEW_KEY}\nPresharedKey = {PSK}\nAllowedIPs = 10.8.1.3/32\n\n"
    )
    assert registry.clients.user_data(NEW_KEY) == {
        "clientName": "Phone",
        "creationDate": "Mon Nov 2 09:05:07 2026",
        "lamneziaUserId": 7,
    }


def test_new_client_keeps_other_records_untouched(
    registry: AwgPeerRegistry, read_fixture: Callable[[str], str]
) -> None:
    registry.add_client(NEW_KEY, PHONE, preshared_key=PSK)

    assert registry.clients.client_ids == [FIRST_PEER, SECOND_PEER, NEW_KEY]
    assert registry.config.serialize().startswith(read_fixture("awg0.conf"))
    # The result is still a table the app parses.
    assert ClientsTable.parse(registry.clients.serialize()).name(NEW_KEY) == "Phone"


def test_adding_the_same_key_again_changes_nothing(registry: AwgPeerRegistry) -> None:
    first = registry.add_client(NEW_KEY, ClientRecord("Phone", CREATED, 7), preshared_key=PSK)
    before = texts(registry)

    again = registry.add_client(NEW_KEY, ClientRecord("Phone", CREATED, 7), preshared_key=PSK)

    assert again == first
    assert texts(registry) == before


def test_reserved_addresses_of_disabled_peers_are_skipped(registry: AwgPeerRegistry) -> None:
    peer = registry.add_client(
        NEW_KEY, PHONE, preshared_key=PSK, reserved=[IPv4Address("10.8.1.3")]
    )

    assert peer.allowed_ips == ("10.8.1.4/32",)


def test_existing_record_gets_the_owner(registry: AwgPeerRegistry) -> None:
    registry.add_client(SECOND_PEER, ClientRecord("Phone", CREATED, 3), preshared_key=PSK)

    assert registry.clients.user_data(SECOND_PEER) == {
        "clientName": "Client 1",
        "lamneziaUserId": 3,
    }


def test_disable_removes_the_peer_and_keeps_the_record(registry: AwgPeerRegistry) -> None:
    registry.disable(FIRST_PEER)

    assert registry.config.find_peer(FIRST_PEER) is None
    assert registry.config.find_peer(SECOND_PEER) is not None
    assert registry.clients.name(FIRST_PEER) == "Ноутбук Анны"


def test_enable_puts_the_peer_back_with_its_address(registry: AwgPeerRegistry) -> None:
    peer = registry.config.find_peer(FIRST_PEER)
    assert peer is not None
    registry.disable(FIRST_PEER)

    registry.enable(peer, ClientRecord("Ноутбук Анны", CREATED))

    assert registry.config.find_peer(FIRST_PEER) == peer
    assert registry.clients.user_data(FIRST_PEER)["creationDate"] == "Mon Nov 2 13:56:07 2026"


def test_enable_recreates_a_record_removed_in_the_app(registry: AwgPeerRegistry) -> None:
    peer = registry.config.find_peer(FIRST_PEER)
    assert peer is not None
    registry.revoke(FIRST_PEER)

    registry.enable(peer, ClientRecord("Laptop", CREATED, 5))

    assert registry.clients.user_data(FIRST_PEER)["lamneziaUserId"] == 5


def test_enable_refuses_an_address_taken_meanwhile(registry: AwgPeerRegistry) -> None:
    peer = registry.config.find_peer(SECOND_PEER)
    assert peer is not None
    registry.disable(SECOND_PEER)
    registry.add_client(NEW_KEY, PHONE, preshared_key=PSK)  # the app reuses .2

    with pytest.raises(PeerConflictError, match=r"10\.8\.1\.2"):
        registry.enable(peer, ClientRecord("Client 1", CREATED))


@pytest.mark.parametrize("change", ["disable", "enable", "revoke"])
def test_changes_are_idempotent(registry: AwgPeerRegistry, change: str) -> None:
    peer = registry.config.find_peer(FIRST_PEER)
    assert peer is not None
    changes: dict[str, Callable[[], None]] = {
        "disable": lambda: registry.disable(FIRST_PEER),
        "enable": lambda: registry.enable(peer, ClientRecord("Ноутбук Анны", CREATED)),
        "revoke": lambda: registry.revoke(FIRST_PEER),
    }
    apply = changes[change]
    apply()
    before = texts(registry)

    apply()

    assert texts(registry) == before


def test_revoke_removes_peer_and_record(registry: AwgPeerRegistry) -> None:
    registry.revoke(FIRST_PEER)

    assert registry.config.find_peer(FIRST_PEER) is None
    assert registry.clients.client_ids == [SECOND_PEER]


def test_rename_changes_only_the_name_in_clients_table(registry: AwgPeerRegistry) -> None:
    config_before = registry.config.serialize()

    registry.rename(FIRST_PEER, "Laptop")
    registry.rename(NEW_KEY, "Unknown")

    assert registry.clients.name(FIRST_PEER) == "Laptop"
    assert NEW_KEY not in registry.clients
    assert registry.config.serialize() == config_before
