"""Identity of an AmneziaWG server group: the leader's files every member copies, and their check.

Members of a group are clones (docs/FALLBACK.md section 3): the client verifies the server key and
the obfuscation parameters, so each member has the same server key, [Interface], subnet, port,
peers and clientsTable as the leading server. A server leaving the group gets clean files.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Interface

from amnezia_core.awg import AwgServerConfig, Peer
from amnezia_core.awg_backup import AwgServerFiles, interface_config
from amnezia_core.awg_install import generate_awg_params
from amnezia_core.clients_table import ClientsTable
from amnezia_core.errors import ConfigFormatError
from amnezia_core.keys import KeyPair, decode_key, generate_preshared_key

# userData fields the app fills with one server's statistics when it lists users
# (UsersController in upstream usersController.cpp); members differ in them by nature.
MEMBER_STATS = frozenset({"latestHandshake", "dataReceived", "dataSent", "allowed_ips"})

_PRIVATE_KEY = "PrivateKey"


class Mismatch(StrEnum):
    """What a member has differently from the group."""

    PRIVATE_KEY = "private_key"
    PRESHARED_KEY = "preshared_key"
    INTERFACE = "interface"
    PEER_MISSING = "peer_missing"
    PEER_EXTRA = "peer_extra"
    PEER_CHANGED = "peer_changed"
    CLIENT_MISSING = "client_missing"
    CLIENT_EXTRA = "client_extra"
    CLIENT_CHANGED = "client_changed"


@dataclass(frozen=True)
class Divergence:
    """One difference of a member: `subject` is an [Interface] entry name or a peer public key.

    Values are left out on purpose: the server key and the preshared key are secrets.
    """

    kind: Mismatch
    subject: str = ""


class AwgGroupIdentity:
    """The leading server's key, [Interface], subnet, port, peers and clients."""

    def __init__(self, leader: AwgServerFiles) -> None:
        config = AwgServerConfig.parse(leader.config)
        if not config.private_key or config.listen_port is None:
            raise ConfigFormatError(
                "awg0.conf of the leading server needs PrivateKey and ListenPort."
            )
        decode_key(leader.preshared_key.strip())
        ClientsTable.parse(leader.clients_table)
        self.files = leader
        self.port = config.listen_port
        self.subnet = config.address

    def member_files(self) -> dict[str, bytes]:
        """What a joining server writes into its container: the leader's files byte for byte."""
        return self.files.by_path()

    def compare(self, member: AwgServerFiles) -> list[Divergence]:
        """Every way the member differs from the group; empty for a clone."""
        leader_config = AwgServerConfig.parse(self.files.config)
        member_config = AwgServerConfig.parse(member.config)
        return [
            *_interface_divergences(leader_config, member_config),
            *_preshared_key_divergences(self.files, member),
            *_peer_divergences(leader_config.peers, member_config.peers),
            *_client_divergences(
                ClientsTable.parse(self.files.clients_table),
                ClientsTable.parse(member.clients_table),
            ),
        ]


def standalone_files(address: IPv4Interface, port: int) -> AwgServerFiles:
    """A clean server leaving a group: a new key, PSK and obfuscation, no peers or clients.

    The address and the port stay: the container runs with them (start.sh, published port).
    """
    config = interface_config(KeyPair.generate().private_key, address, port, generate_awg_params())
    return AwgServerFiles(
        config.serialize(), ClientsTable([]).serialize(), generate_preshared_key()
    )


def has_clients(files: AwgServerFiles) -> bool:
    """The server has peers or clients someone may still use: replacing them needs a backup."""
    config = AwgServerConfig.parse(files.config)
    return bool(config.peers or ClientsTable.parse(files.clients_table).client_ids)


def _interface_divergences(leader: AwgServerConfig, member: AwgServerConfig) -> list[Divergence]:
    expected, actual = _interface_entries(leader), _interface_entries(member)
    names = [*expected, *(name for name in actual if name not in expected)]
    return [
        Divergence(Mismatch.PRIVATE_KEY)
        if name == _PRIVATE_KEY
        else Divergence(Mismatch.INTERFACE, name)
        for name in names
        if expected.get(name) != actual.get(name)
    ]


def _interface_entries(config: AwgServerConfig) -> dict[str, str]:
    """All [Interface] entries, I1-I5 also from the comments where upstream keeps them."""
    return {**dict(config.interface.entries()), **config.awg_params}


def _preshared_key_divergences(leader: AwgServerFiles, member: AwgServerFiles) -> list[Divergence]:
    if leader.preshared_key.strip() == member.preshared_key.strip():
        return []
    return [Divergence(Mismatch.PRESHARED_KEY)]


def _peer_divergences(leader: list[Peer], member: list[Peer]) -> list[Divergence]:
    expected = {peer.public_key: peer for peer in leader}
    actual = {peer.public_key: peer for peer in member}
    return _keyed_divergences(
        expected,
        actual,
        (Mismatch.PEER_MISSING, Mismatch.PEER_EXTRA, Mismatch.PEER_CHANGED),
    )


def _client_divergences(leader: ClientsTable, member: ClientsTable) -> list[Divergence]:
    return _keyed_divergences(
        _client_records(leader),
        _client_records(member),
        (Mismatch.CLIENT_MISSING, Mismatch.CLIENT_EXTRA, Mismatch.CLIENT_CHANGED),
    )


def _client_records(table: ClientsTable) -> dict[str, dict[str, object]]:
    return {
        client_id: {
            key: value
            for key, value in table.user_data(client_id).items()
            if key not in MEMBER_STATS
        }
        for client_id in table.client_ids
    }


def _keyed_divergences(
    expected: Mapping[str, object],
    actual: Mapping[str, object],
    kinds: tuple[Mismatch, Mismatch, Mismatch],
) -> list[Divergence]:
    """Missing, extra and changed records by key; the order of records does not matter."""
    missing, extra, changed = kinds
    return [
        *(Divergence(missing, key) for key in expected if key not in actual),
        *(Divergence(extra, key) for key in actual if key not in expected),
        *(
            Divergence(changed, key)
            for key in expected
            if key in actual and expected[key] != actual[key]
        ),
    ]
