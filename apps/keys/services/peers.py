"""Adding, disabling, enabling and revoking peers in a WireGuard-family container of a server."""

from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager, ExitStack
from ipaddress import IPv4Address
from typing import Protocol

from django.utils import timezone
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from amnezia_core.awg import AwgServerConfig, Peer
from amnezia_core.awg_dump import parse_dump
from amnezia_core.awg_export import AwgServerSettings
from amnezia_core.awg_peers import AwgPeerRegistry
from amnezia_core.clients_table import ClientRecord, ClientsTable
from amnezia_core.containers import AWG2, WgContainer
from amnezia_core.errors import ConfigFormatError
from amnezia_core.keys import KeyPair
from apps.keys.errors import PeerError, PeerErrorCode, explained_format_errors
from apps.keys.services.swaps import Swap, SwapResult
from apps.ssh.errors import RemoteFileNotFoundError
from apps.ssh.services import DockerContainer, RemoteHost

# The server config holds the server private key.
_FILE_MODE = 0o600


def _sync_script(kind: WgContainer) -> str:
    """Upstream `<tool> syncconf <iface> <(<tool>-quick strip <conf>)` runs in bash; same in sh.

    syncconf changes only the peers that differ, so other clients keep their sessions.
    """
    return (
        "set -e\n"
        "tmp=$(mktemp)\n"
        "trap 'rm -f -- \"$tmp\"' EXIT\n"
        f'{kind.tool}-quick strip {kind.config_path} > "$tmp"\n'
        f'{kind.tool} syncconf {kind.interface} "$tmp"\n'
    )


def read_registry(host: RemoteHost, kind: WgContainer = AWG2) -> AwgPeerRegistry:
    """The server config and clientsTable as they are now; only reads, without the lock."""
    container = host.container(kind.name)
    config_text = container.read_file(kind.config_path).decode()
    with explained_format_errors():
        return AwgPeerRegistry.parse(config_text, _read_clients_table(container, kind))


def _read_clients_table(container: DockerContainer, kind: WgContainer) -> str:
    try:
        return container.read_file(kind.clients_table_path).decode()
    except RemoteFileNotFoundError:
        return ""  # never written yet; the app treats it as empty too


class StepRecorder(Protocol):
    """Where progress goes; `OperationContext` in a worker."""

    def step(self, title: str | Promise) -> AbstractContextManager[None]: ...

    def log(self, line: str) -> None: ...


class PeerManager:
    """Peer changes by the safe write protocol (DESIGN.md 5.4).

    Each call takes the server lock, re-reads the server config and clientsTable (the Amnezia
    app may have changed them), writes only what changed and applies the config with syncconf.
    Calls are idempotent, so a failed operation can be run again. The same code serves
    AmneziaWG 2.0, legacy AmneziaWG and WireGuard containers.
    """

    def __init__(self, host: RemoteHost, steps: StepRecorder, kind: WgContainer = AWG2) -> None:
        self._host = host
        self._kind = kind
        self._container: DockerContainer = host.container(kind.name)
        self._steps = steps

    def add(
        self,
        public_key: str,
        name: str,
        *,
        user_id: int | None = None,
        reserved: Iterable[IPv4Address] = (),
    ) -> Peer:
        """Add a client with the server preshared key, as the app does; return its peer.

        `reserved` are addresses of the panel's disabled keys on this container.
        """
        reserved = tuple(reserved)
        return self._change(
            lambda registry: registry.add_client(
                public_key,
                ClientRecord(name, timezone.localtime(), user_id),
                preshared_key=self.preshared_key(),
                reserved=reserved,
            )
        )

    def enable(self, peer: Peer, name: str, *, user_id: int | None = None) -> None:
        """Put a disabled peer back with the address and keys stored in the panel."""
        record = ClientRecord(name, timezone.localtime(), user_id)
        self._change(lambda registry: registry.enable(peer, record))

    def disable(self, public_key: str) -> None:
        """Drop the peer from the server config; the panel keeps its keys to enable it later."""
        self._change(lambda registry: registry.disable(public_key))

    def rename(self, public_key: str, name: str) -> None:
        self._change(lambda registry: registry.rename(public_key, name))

    def revoke(self, public_key: str) -> None:
        self._change(lambda registry: registry.revoke(public_key))

    def replace(
        self, swaps: Sequence[Swap], *, reserved: Iterable[IPv4Address] = ()
    ) -> SwapResult[Peer]:
        """Add the new peers and revoke the old ones in one change of awg0.conf and clientsTable.

        The new peer is added first: when there is no free address, the old one stays.
        """
        reserved = tuple(reserved)

        def change(registry: AwgPeerRegistry) -> SwapResult[Peer]:
            preshared_key, result = self.preshared_key(), SwapResult[Peer]()
            for swap in swaps:
                try:
                    with explained_format_errors():
                        peer = registry.add_client(
                            swap.new, swap.record, preshared_key=preshared_key, reserved=reserved
                        )
                except PeerError as error:
                    result.failed[swap.new] = error.message
                    continue
                if swap.old:
                    registry.revoke(swap.old)
                result.done[swap.new] = peer
            return result

        return self._change(change)

    def server_settings(self) -> tuple[AwgServerSettings, str]:
        """Server half of client profiles: settings from the config and the server public key."""
        with self._steps.step(_("Read the server settings")):
            text = self._container.read_file(self._kind.config_path).decode()
            with explained_format_errors():
                config = AwgServerConfig.parse(text)
                if not config.private_key:
                    raise ConfigFormatError("the server config has no PrivateKey")
                public_key = KeyPair.from_private_key(config.private_key).public_key
                return AwgServerSettings.from_config(config), public_key

    def _change[T](self, change: Callable[[AwgPeerRegistry], T]) -> T:
        with ExitStack() as held:
            with self._steps.step(_("Lock the server")):
                held.enter_context(self._host.lock())
            with self._steps.step(_("Read the protocol configuration")):
                config_text = self._container.read_file(self._kind.config_path).decode()
                clients_text = _read_clients_table(self._container, self._kind)
                registry, result = _apply(change, config_text, clients_text)
            config_changed = registry.config.serialize() != config_text
            # A retry after a failed syncconf finds the file already written but not applied.
            if config_changed or self._live_peers() != _peers_of(registry):
                with self._steps.step(_("Update the server configuration")):
                    if config_changed:
                        self._write(self._kind.config_path, registry.config.serialize())
                    self._container.run(_sync_script(self._kind))
                    self._check_live_peers(registry)
            if registry.clients.serialize() != ClientsTable.parse(clients_text).serialize():
                with self._steps.step(_("Update the client list")):
                    self._write(self._kind.clients_table_path, registry.clients.serialize())
            return result

    def preshared_key(self) -> str:
        """The key the container gives every client (wireguard_psk.key)."""
        return self._container.read_file(self._kind.psk_path).decode().strip()

    def _write(self, path: str, text: str) -> None:
        self._container.write_file_atomic(path, text.encode(), _FILE_MODE)

    def _live_peers(self) -> set[str]:
        dump = parse_dump(self._container.run(f"{self._kind.tool} show all dump"))
        return {peer.public_key for peer in dump if peer.interface == self._kind.interface}

    def _check_live_peers(self, registry: AwgPeerRegistry) -> None:
        """The interface must now have exactly the peers of the file."""
        live, expected = self._live_peers(), _peers_of(registry)
        self._steps.log(
            _("Peers on %(interface)s: %(count)d.")
            % {"interface": self._kind.interface, "count": len(live)}
        )
        if live != expected:
            detail = f"missing {len(expected - live)}, unexpected {len(live - expected)}"
            raise PeerError(PeerErrorCode.NOT_APPLIED, detail)


def _peers_of(registry: AwgPeerRegistry) -> set[str]:
    return {peer.public_key for peer in registry.config.peers}


def _apply[T](
    change: Callable[[AwgPeerRegistry], T], config_text: str, clients_text: str
) -> tuple[AwgPeerRegistry, T]:
    with explained_format_errors():
        registry = AwgPeerRegistry.parse(config_text, clients_text)
        return registry, change(registry)
