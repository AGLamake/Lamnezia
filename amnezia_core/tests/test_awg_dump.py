from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from amnezia_core.awg_dump import PeerStats, parse_dump
from amnezia_core.errors import DumpFormatError
from amnezia_core.tests.conftest import FIRST_PEER, SECOND_PEER


def test_parses_peers_and_skips_interface_line(read_fixture: Callable[[str], str]) -> None:
    active, idle = parse_dump(read_fixture("dump.txt"))

    assert active == PeerStats(
        interface="awg0",
        public_key=FIRST_PEER,
        endpoint="203.0.113.7:51820",
        allowed_ips=("10.8.1.1/32",),
        latest_handshake=datetime(2026, 11, 1, 13, 56, 7, tzinfo=UTC),
        rx_bytes=1310720,
        tx_bytes=16148070,
        persistent_keepalive=None,
    )
    assert idle.public_key == SECOND_PEER
    assert idle.endpoint is None
    assert idle.latest_handshake is None
    assert idle.persistent_keepalive == 25


def test_each_interface_has_own_header_line() -> None:
    text = "awg0\tp\tP\t1\toff\nwg0\tp\tP\t2\toff\nwg0\tk\t(none)\t(none)\t(none)\t0\t0\t0\toff\n\n"

    (peer,) = parse_dump(text)

    assert peer.interface == "wg0"
    assert peer.allowed_ips == ()


@pytest.mark.parametrize(
    "peer_line",
    ["awg0\tk\tpsk\t(none)", "awg0\tk\tpsk\t(none)\t(none)\tsoon\t0\t0\toff"],
)
def test_rejects_malformed_peer_line(peer_line: str) -> None:
    with pytest.raises(DumpFormatError):
        parse_dump(f"awg0\tp\tP\t1\toff\n{peer_line}\n")
