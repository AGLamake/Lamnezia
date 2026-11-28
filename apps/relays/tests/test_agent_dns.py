"""The relay agent's lookups of the own domains through the relay's resolver."""

import socket
import struct
import threading
from collections.abc import Iterator

import pytest

from apps.relays.agent.relay_agent import ResolverError, dns_addresses, dns_query, resolve

IDENT = 0x1234
QUERY = dns_query("example.ru", IDENT)


def answer(*addresses: str, ident: int = IDENT, code: int = 0, truncated: bool = False) -> bytes:
    """An answer to QUERY with A records whose names point back to the question."""
    flags = 0x8180 | code | (0x0200 if truncated else 0)
    header = struct.pack("!6H", ident, flags, 1, len(addresses), 0, 0)
    records = b"".join(
        struct.pack("!3HIH", 0xC00C, 1, 1, 300, 4) + socket.inet_aton(address)
        for address in addresses
    )
    return header + QUERY[12:] + records


def test_the_query_asks_for_the_a_records_with_recursion() -> None:
    assert QUERY[:12] == struct.pack("!6H", IDENT, 0x0100, 1, 0, 0, 0)
    assert QUERY[12:] == b"\x07example\x02ru\x00\x00\x01\x00\x01"


def test_the_addresses_of_an_answer() -> None:
    packet = answer("93.184.215.15", "93.184.215.14")

    assert dns_addresses(packet, IDENT) == ("93.184.215.14", "93.184.215.15")


def test_records_other_than_a_are_skipped() -> None:
    header = struct.pack("!6H", IDENT, 0x8180, 1, 2, 0, 0)
    cname = struct.pack("!3HIH", 0xC00C, 5, 1, 300, 2) + b"\xc0\x0c"
    record = struct.pack("!3HIH", 0xC00C, 1, 1, 300, 4) + socket.inet_aton("93.184.215.14")

    assert dns_addresses(header + QUERY[12:] + cname + record, IDENT) == ("93.184.215.14",)


def test_a_name_that_does_not_exist_has_no_addresses() -> None:
    assert dns_addresses(answer(code=3), IDENT) == ()


@pytest.mark.parametrize(
    ("packet", "message"),
    [
        (answer(code=2), "failed the lookup with code 2"),
        (answer("93.184.215.14", ident=IDENT + 1), "answered another query"),
        (answer("93.184.215.14")[:-3], "broken answer"),
        (b"\x12", "broken answer"),
    ],
)
def test_a_failed_or_broken_answer_is_a_resolver_failure(packet: bytes, message: str) -> None:
    with pytest.raises(ResolverError, match=message):
        dns_addresses(packet, IDENT)


class FakeResolver:
    """A resolver on the loopback that answers every query over UDP, and over TCP when its UDP
    answers are cut short."""

    def __init__(self, addresses: tuple[str, ...], truncated: bool = False) -> None:
        self.addresses = addresses
        self.truncated = truncated
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.address: tuple[str, int] = self.udp.getsockname()
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.bind(self.address)
        self.tcp.listen()
        self._threads = [
            threading.Thread(target=self._serve_udp, daemon=True),
            threading.Thread(target=self._serve_tcp, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def close(self) -> None:
        self.udp.close()
        self.tcp.close()

    def _reply(self, query: bytes, truncated: bool) -> bytes:
        (ident,) = struct.unpack_from("!H", query)
        if truncated:
            return answer(ident=ident, truncated=True)
        return answer(*self.addresses, ident=ident)

    def _serve_udp(self) -> None:
        try:
            query, client = self.udp.recvfrom(512)
            self.udp.sendto(self._reply(query, self.truncated), client)
        except OSError:
            pass

    def _serve_tcp(self) -> None:
        try:
            connection, _ = self.tcp.accept()
            with connection:
                (length,) = struct.unpack("!H", connection.recv(2))
                reply = self._reply(connection.recv(length), truncated=False)
                connection.sendall(struct.pack("!H", len(reply)) + reply)
        except OSError:
            pass


@pytest.fixture
def server() -> Iterator[FakeResolver]:
    found = FakeResolver(("93.184.215.14",))
    yield found
    found.close()


def test_resolve_asks_the_relay_resolver(server: FakeResolver) -> None:
    assert resolve("example.ru", server.address, 2) == ("93.184.215.14",)


def test_an_answer_cut_short_is_asked_again_over_tcp() -> None:
    server = FakeResolver(("5.8.1.1", "5.8.1.2"), truncated=True)
    try:
        assert resolve("example.ru", server.address, 2) == ("5.8.1.1", "5.8.1.2")
    finally:
        server.close()


def test_a_silent_resolver_is_a_failure_without_the_name() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent:
        silent.bind(("127.0.0.1", 0))
        with pytest.raises(ResolverError, match="does not answer") as caught:
            resolve("example.ru", silent.getsockname(), 0.2)

    assert "example" not in str(caught.value)
