from concurrent.futures import ThreadPoolExecutor

import pytest

from amnezia_core.awg import (
    AWG_CLIENTS_TABLE_PATH,
    AWG_CONFIG_PATH,
    AWG_PSK_PATH,
    AwgServerConfig,
    Peer,
)
from amnezia_core.clients_table import ClientsTable
from amnezia_core.containers import AWG_LEGACY, WIREGUARD, WgContainer
from amnezia_core.keys import KeyPair
from apps.keys.errors import PeerError, PeerErrorCode
from apps.keys.services.peers import PeerManager
from apps.keys.tests.fake_server import FakeAwgServer
from apps.keys.tests.steps import FIXTURES, RecordedSteps

FIRST_PEER = "jhkQdvDcgDYudvey3hItwkaQoDy/bLT6QnsyYHwCIlo="
SECOND_PEER = "Q2+/5jIlpVjLMKDArTagVYSPsI5fcqPNpSem9NMi50s="
PSK = "u+xLktHw90auwUVtjZEJVpNCbfbFK8GNmGegY/Z0XYs="


@pytest.fixture
def server() -> FakeAwgServer:
    server = FakeAwgServer(
        {
            AWG_CONFIG_PATH: (FIXTURES / "awg0.conf").read_text(encoding="utf-8"),
            AWG_CLIENTS_TABLE_PATH: (FIXTURES / "clientsTable.json").read_text(encoding="utf-8"),
            AWG_PSK_PATH: f"{PSK}\n",
        }
    )
    server.live = {FIRST_PEER: 1_793_000_000, SECOND_PEER: 1_793_000_100}
    return server


def manager_for(server: FakeAwgServer, steps: RecordedSteps | None = None) -> PeerManager:
    return PeerManager(server.host(), steps or RecordedSteps())


def config_of(server: FakeAwgServer) -> AwgServerConfig:
    return AwgServerConfig.parse(server.files[AWG_CONFIG_PATH])


def clients_of(server: FakeAwgServer) -> ClientsTable:
    return ClientsTable.parse(server.files[AWG_CLIENTS_TABLE_PATH])


def writes_to(server: FakeAwgServer, path: str) -> int:
    return sum(f"target={path}" in command for command in server.commands)


def test_add_writes_both_files_and_applies_them(server: FakeAwgServer) -> None:
    steps = RecordedSteps()
    key = KeyPair.generate().public_key

    peer = manager_for(server, steps).add(key, "Phone", user_id=7)

    assert peer == Peer(key, ("10.8.1.3/32",), PSK)
    assert config_of(server).find_peer(key) == peer
    assert clients_of(server).user_data(key)["lamneziaUserId"] == 7
    assert key in server.live
    assert steps.titles == [
        "Lock the server",
        "Read the protocol configuration",
        "Update the server configuration",
        "Update the client list",
    ]
    assert not server.lock.locked()


def test_add_does_not_reconnect_other_clients(server: FakeAwgServer) -> None:
    before = dict(server.live)

    manager_for(server).add(KeyPair.generate().public_key, "Phone")

    assert {key: server.live[key] for key in before} == before
    commands = "\n".join(server.commands)
    assert "syncconf" in commands
    for restart in ("restart", "awg-quick down", "awg-quick up", "setconf"):
        assert restart not in commands


def test_parallel_adds_keep_every_peer() -> None:
    server = FakeAwgServer(
        {
            AWG_CONFIG_PATH: (FIXTURES / "awg0.conf").read_text(encoding="utf-8"),
            AWG_CLIENTS_TABLE_PATH: (FIXTURES / "clientsTable.json").read_text(encoding="utf-8"),
            AWG_PSK_PATH: PSK,
        },
        read_delay=0.005,
    )
    keys = [KeyPair.generate().public_key for _ in range(10)]

    with ThreadPoolExecutor(max_workers=10) as pool:
        peers = list(pool.map(lambda key: manager_for(server).add(key, "Phone"), keys))

    config = config_of(server)
    assert {peer.public_key for peer in config.peers} == {FIRST_PEER, SECOND_PEER, *keys}
    assert len({address for peer in config.peers for address in peer.addresses}) == 12
    assert {config.find_peer(peer.public_key) for peer in peers} == set(peers)
    assert set(clients_of(server).client_ids) == {FIRST_PEER, SECOND_PEER, *keys}
    assert set(server.live) == {FIRST_PEER, SECOND_PEER, *keys}


def test_disable_removes_the_live_peer_and_keeps_the_record(server: FakeAwgServer) -> None:
    table = server.files[AWG_CLIENTS_TABLE_PATH]

    manager_for(server).disable(FIRST_PEER)

    assert config_of(server).find_peer(FIRST_PEER) is None
    assert set(server.live) == {SECOND_PEER}
    assert server.files[AWG_CLIENTS_TABLE_PATH] == table
    assert writes_to(server, AWG_CLIENTS_TABLE_PATH) == 0


def test_enable_restores_the_disabled_peer(server: FakeAwgServer) -> None:
    peer = config_of(server).find_peer(FIRST_PEER)
    assert peer is not None
    manager = manager_for(server)
    manager.disable(FIRST_PEER)

    manager.enable(peer, "Ноутбук Анны")

    assert config_of(server).find_peer(FIRST_PEER) == peer
    assert FIRST_PEER in server.live


def test_revoke_removes_peer_and_record(server: FakeAwgServer) -> None:
    manager_for(server).revoke(FIRST_PEER)

    assert config_of(server).find_peer(FIRST_PEER) is None
    assert FIRST_PEER not in clients_of(server)
    assert FIRST_PEER not in server.live


def test_repeating_a_change_writes_nothing(server: FakeAwgServer) -> None:
    manager_for(server).revoke(FIRST_PEER)
    server.commands.clear()
    steps = RecordedSteps()

    manager_for(server, steps).revoke(FIRST_PEER)

    assert steps.titles == ["Lock the server", "Read the protocol configuration"]
    assert not any("target=" in command or "syncconf" in command for command in server.commands)


def test_retry_applies_a_config_written_before_a_failed_sync(server: FakeAwgServer) -> None:
    manager_for(server).revoke(FIRST_PEER)
    server.live[FIRST_PEER] = 0  # the previous attempt wrote the file, syncconf failed

    manager_for(server).revoke(FIRST_PEER)

    assert set(server.live) == {SECOND_PEER}


def test_missing_clients_table_is_created(server: FakeAwgServer) -> None:
    del server.files[AWG_CLIENTS_TABLE_PATH]
    key = KeyPair.generate().public_key

    manager_for(server).add(key, "Phone")

    assert clients_of(server).client_ids == [key]


def test_peers_not_applied_by_the_interface_fail(
    server: FakeAwgServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "sync", lambda: None)

    with pytest.raises(PeerError) as error:
        manager_for(server).add(KeyPair.generate().public_key, "Phone")

    assert error.value.code == PeerErrorCode.NOT_APPLIED
    assert not server.lock.locked()


def test_taken_address_stops_enable_before_any_write(server: FakeAwgServer) -> None:
    peer = config_of(server).find_peer(SECOND_PEER)
    assert peer is not None
    manager = manager_for(server)
    manager.disable(SECOND_PEER)
    manager.add(KeyPair.generate().public_key, "Phone")  # gets 10.8.1.2 again
    server.commands.clear()

    with pytest.raises(PeerError) as error:
        manager.enable(peer, "Client 1")

    assert error.value.code == PeerErrorCode.ADDRESS_TAKEN
    assert not any("target=" in command for command in server.commands)


def test_reserved_addresses_are_not_handed_out(server: FakeAwgServer) -> None:
    peer = config_of(server).find_peer(SECOND_PEER)
    assert peer is not None
    manager = manager_for(server)
    manager.disable(SECOND_PEER)

    added = manager.add(KeyPair.generate().public_key, "Phone", reserved=peer.addresses)

    assert added.allowed_ips == ("10.8.1.3/32",)


def test_broken_config_is_reported(server: FakeAwgServer) -> None:
    server.files[AWG_CONFIG_PATH] = "[Peer]\nPublicKey = x\n"

    with pytest.raises(PeerError) as error:
        manager_for(server).disable(FIRST_PEER)

    assert error.value.code == PeerErrorCode.SERVER_FILES_INVALID
    assert error.value.message


@pytest.mark.parametrize(
    ("kind", "fixture"),
    [(AWG_LEGACY, "awg_legacy_wg0.conf"), (WIREGUARD, "wireguard_wg0.conf")],
    ids=["awg-legacy", "wireguard"],
)
def test_other_wireguard_containers_issue_and_revoke(kind: WgContainer, fixture: str) -> None:
    server = FakeAwgServer(
        {
            kind.config_path: (FIXTURES / fixture).read_text(encoding="utf-8"),
            kind.psk_path: PSK,
        },
        kind=kind,
    )
    manager = PeerManager(server.host(), RecordedSteps(), kind)
    key = KeyPair.generate().public_key

    peer = manager.add(key, "Phone", user_id=1)

    assert peer.allowed_ips == ("10.8.1.2/32",)
    assert set(server.live) == {FIRST_PEER, key}
    assert ClientsTable.parse(server.files[kind.clients_table_path]).client_ids == [key]
    commands = "\n".join(server.commands)
    assert f"docker exec -i {kind.name} " in commands
    assert f"wg-quick strip {kind.config_path}" in commands
    assert "wg syncconf wg0" in commands
    assert "wg show all dump" in commands
    assert "awg syncconf" not in commands and "awg-quick" not in commands

    manager.revoke(key)

    assert set(server.live) == {FIRST_PEER}
    assert key not in ClientsTable.parse(server.files[kind.clients_table_path])
