"""Peer changes of an AmneziaWG 2.0 container: awg0.conf and clientsTable edited together."""

from collections.abc import Iterable
from ipaddress import IPv4Address

from amnezia_core.awg import AwgServerConfig, Peer
from amnezia_core.clients_table import CLIENT_NAME, ClientRecord, ClientsTable
from amnezia_core.errors import PeerConflictError
from amnezia_core.ip_pool import IpPool


class AwgPeerRegistry:
    """Server config and client registry of one container as read from the server.

    Every change is idempotent: applied again to its own result it changes nothing, so an
    operation that failed halfway can simply be run again.
    """

    def __init__(self, config: AwgServerConfig, clients: ClientsTable) -> None:
        self.config = config
        self.clients = clients

    @classmethod
    def parse(cls, config_text: str, clients_text: str) -> "AwgPeerRegistry":
        return cls(AwgServerConfig.parse(config_text), ClientsTable.parse(clients_text))

    def add_client(
        self,
        public_key: str,
        client: ClientRecord,
        *,
        preshared_key: str,
        reserved: Iterable[IPv4Address] = (),
    ) -> Peer:
        """Give a new client the next free address; a key already on the server keeps its peer.

        `reserved` are addresses of disabled peers: they are not in awg0.conf but must stay free.
        """
        peer = self.config.find_peer(public_key)
        if peer is None:
            pool = IpPool(self.config.address, [*self._addresses_in_use(), *reserved])
            peer = Peer(public_key, (f"{pool.next_address()}/32",), preshared_key)
            self.config.add_peer(peer)
        self.clients.register(public_key, client)
        return peer

    def enable(self, peer: Peer, client: ClientRecord) -> None:
        """Put a disabled peer back with its own address and keys: the client config stays valid."""
        if self.config.find_peer(peer.public_key) is None:
            taken = set(peer.addresses) & self._addresses_in_use()
            if taken:
                addresses = ", ".join(str(address) for address in sorted(taken))
                raise PeerConflictError(f"Address {addresses} is used by another peer.")
            self.config.add_peer(peer)
        self.clients.register(peer.public_key, client)

    def disable(self, public_key: str) -> None:
        """Take the peer out of awg0.conf.

        Its clientsTable record stays: the app keeps the name and does not drop records
        without a peer.
        """
        self.config.remove_peer(public_key)

    def rename(self, public_key: str, name: str) -> None:
        """Set the name the app shows, as its rename does; unknown clients are skipped."""
        if public_key in self.clients:
            self.clients.update(public_key, {CLIENT_NAME: name})

    def revoke(self, public_key: str) -> None:
        self.config.remove_peer(public_key)
        self.clients.remove(public_key)

    def _addresses_in_use(self) -> set[IPv4Address]:
        return {address for peer in self.config.peers for address in peer.addresses}
