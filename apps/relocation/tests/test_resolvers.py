import socket
import struct
import threading
from collections.abc import Iterator

import pytest

from apps.relocation.services.resolvers import (
    Answer,
    DnsQuery,
    Resolvers,
    UnreachableResolverError,
)

NAME = "panel.example.com"


def reply(query: DnsQuery, *records: tuple[int, bytes], code: int = 0) -> bytes:
    """A resolver reply to the query: the question again, then answers pointing to it."""
    packet = query.packet()
    ident = struct.unpack_from("!H", packet)[0]
    header = struct.pack("!HHHHHH", ident, 0x8180 | code, 1, len(records), 0, 0)
    body = packet[12:]
    for kind, data in records:
        body += b"\xc0\x0c" + struct.pack("!HHIH", kind, 1, 300, len(data)) + data
    return header + body


def test_a_query_asks_for_the_a_record_of_the_name() -> None:
    packet = DnsQuery(NAME).packet()

    assert packet[2:12] == b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    assert packet[12:] == b"\x05panel\x07example\x03com\x00\x00\x01\x00\x01"


def test_a_records_are_read_and_other_records_skipped() -> None:
    query = DnsQuery(NAME)
    cname = b"\x03cdn\xc0\x12"

    found = query.addresses(
        reply(query, (5, cname), (1, bytes([198, 51, 100, 7])), (1, bytes([203, 0, 113, 10])))
    )

    assert found == {"198.51.100.7", "203.0.113.10"}


def test_a_name_that_does_not_exist_has_no_records() -> None:
    query = DnsQuery(NAME)

    assert query.addresses(reply(query, code=3)) == set()


@pytest.mark.parametrize("broken", ["refused", "foreign", "short"])
def test_a_reply_that_cannot_be_used_is_unreachable(broken: str) -> None:
    query = DnsQuery(NAME)
    data = reply(query, (1, bytes([198, 51, 100, 7])))
    if broken == "refused":
        data = reply(query, code=5)
    elif broken == "foreign":
        data = bytes([data[0] ^ 0xFF]) + data[1:]
    else:
        data = data[:20]

    with pytest.raises(UnreachableResolverError):
        query.addresses(data)


@pytest.fixture
def resolver() -> Iterator[tuple[str, int]]:
    """A local UDP resolver that gives one A record to any query."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))

    def serve() -> None:
        packet, peer = sock.recvfrom(512)
        answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + bytes([198, 51, 100, 7])
        sock.sendto(
            packet[:2] + b"\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00" + packet[12:] + answer, peer
        )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield sock.getsockname()
    thread.join(timeout=2)
    sock.close()


def test_a_resolver_is_asked_over_udp(
    resolver: tuple[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("apps.relocation.services.resolvers.DNS_PORT", resolver[1])

    assert DnsQuery(NAME).ask(resolver[0]) == {"198.51.100.7"}


@pytest.mark.parametrize("server", ["not-an-address", "203.0.113.1"])
def test_a_resolver_that_does_not_answer_is_unreachable(server: str) -> None:
    with pytest.raises(UnreachableResolverError):
        DnsQuery(NAME, timeout=0.2).ask(server)


def test_resolvers_answer_on_their_own() -> None:
    def public(server: str, name: str) -> set[str]:
        if server == "8.8.8.8":
            raise UnreachableResolverError(server)
        return {"198.51.100.7"}

    found = Resolvers(
        [" system", "1.1.1.1", "8.8.8.8", "1.1.1.1", ""],
        system=lambda name: {"203.0.113.10"},
        public=public,
    ).ask(NAME)

    assert found == [
        Answer("system", frozenset({"203.0.113.10"})),
        Answer("1.1.1.1", frozenset({"198.51.100.7"})),
        Answer("8.8.8.8", reachable=False),
    ]
