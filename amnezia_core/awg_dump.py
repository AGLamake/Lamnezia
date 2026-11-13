"""Peer counters from `awg show all dump` (tab-separated, same layout as `wg show all dump`)."""

from dataclasses import dataclass
from datetime import UTC, datetime

from amnezia_core.errors import DumpFormatError

# interface, public-key, preshared-key, endpoint, allowed-ips, latest-handshake, rx, tx, keepalive
_PEER_FIELDS = 9
_NONE = "(none)"


@dataclass(frozen=True)
class PeerStats:
    """Live state of one peer. `rx_bytes` is what the server received, i.e. client upload."""

    interface: str
    public_key: str
    endpoint: str | None
    allowed_ips: tuple[str, ...]
    latest_handshake: datetime | None
    rx_bytes: int
    tx_bytes: int
    persistent_keepalive: int | None


def parse_dump(text: str) -> list[PeerStats]:
    """Peers of all interfaces. The first line of each interface describes the interface itself."""
    seen_interfaces: set[str] = set()
    peers = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if fields[0] not in seen_interfaces:
            seen_interfaces.add(fields[0])
            continue
        peers.append(_parse_peer(fields))
    return peers


def _parse_peer(fields: list[str]) -> PeerStats:
    if len(fields) != _PEER_FIELDS:
        raise DumpFormatError(f"Peer line must have {_PEER_FIELDS} fields, got {len(fields)}.")
    interface, public_key, _psk, endpoint, allowed_ips, handshake, rx, tx, keepalive = fields
    try:
        handshake_at = int(handshake)
        return PeerStats(
            interface=interface,
            public_key=public_key,
            endpoint=None if endpoint == _NONE else endpoint,
            allowed_ips=() if allowed_ips == _NONE else tuple(allowed_ips.split(",")),
            latest_handshake=datetime.fromtimestamp(handshake_at, UTC) if handshake_at else None,
            rx_bytes=int(rx),
            tx_bytes=int(tx),
            persistent_keepalive=None if keepalive == "off" else int(keepalive),
        )
    except ValueError as error:
        raise DumpFormatError(f"Unexpected value in peer line: {error}") from error
