"""DNS resolvers the domain check of a move asks for A records: the system one and public ones.

A public resolver is asked directly by its IP address over UDP with a plain RFC 1035 query, so
each one gives its own answer, not what the system cache remembers. A resolver that does not
answer is marked unreachable and does not count in the decision.
"""

import ipaddress
import secrets
import socket
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

SYSTEM = "system"
DNS_PORT = 53
QUERY_TIMEOUT = 2.0

_HEADER = struct.Struct("!HHHHHH")
_RECORD = struct.Struct("!HHIH")
_TYPE_A = 1
_CLASS_IN = 1
_RECURSION_DESIRED = 0x0100
_RESPONSE = 0x8000
_NXDOMAIN = 3


class UnreachableResolverError(Exception):
    """The resolver did not answer, or its answer cannot be read."""


@dataclass(frozen=True)
class Answer:
    """What one resolver answered: its A records, or that it could not be reached."""

    resolver: str
    addresses: frozenset[str] = field(default_factory=frozenset)
    reachable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolver": self.resolver,
            "addresses": sorted(self.addresses),
            "reachable": self.reachable,
        }


class DnsQuery:
    """One A query for a name, sent to a resolver by its IP address."""

    def __init__(self, name: str, timeout: float = QUERY_TIMEOUT) -> None:
        self._name = name
        self._timeout = timeout
        self._ident = secrets.randbelow(0x10000)

    def ask(self, server: str) -> set[str]:
        """The A records the resolver gives; an empty set when the name has none."""
        try:
            address = ipaddress.ip_address(server)
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET  # noqa: PLR2004
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.settimeout(self._timeout)
                sock.connect((server, DNS_PORT))
                sock.send(self.packet())
                return self.addresses(sock.recv(4096))
        except (OSError, ValueError, UnicodeError) as error:
            raise UnreachableResolverError(server) from error

    def packet(self) -> bytes:
        labels = self._name.rstrip(".").encode("idna").split(b".")
        if any(not label or len(label) > 63 for label in labels):  # noqa: PLR2004
            raise ValueError(self._name)
        question = b"".join(bytes([len(label)]) + label for label in labels) + b"\0"
        header = _HEADER.pack(self._ident, _RECURSION_DESIRED, 1, 0, 0, 0)
        return header + question + struct.pack("!HH", _TYPE_A, _CLASS_IN)

    def addresses(self, reply: bytes) -> set[str]:
        """IPv4 addresses from the answer section of a reply to this query."""
        try:
            return self._read(reply)
        except (IndexError, struct.error) as error:
            raise UnreachableResolverError("malformed reply") from error

    def _read(self, reply: bytes) -> set[str]:
        ident, flags, questions, answers, _, _ = _HEADER.unpack_from(reply)
        if ident != self._ident or not flags & _RESPONSE:
            raise UnreachableResolverError("not a reply to the query")
        code = flags & 0xF
        if code == _NXDOMAIN:
            return set()
        if code:
            raise UnreachableResolverError(f"error code {code}")
        offset = _HEADER.size
        for _ in range(questions):
            offset = _skip_name(reply, offset) + 4
        found = set()
        for _ in range(answers):
            offset = _skip_name(reply, offset)
            kind, klass, _, length = _RECORD.unpack_from(reply, offset)
            offset += _RECORD.size
            data = reply[offset : offset + length]
            if kind == _TYPE_A and klass == _CLASS_IN and len(data) == 4:  # noqa: PLR2004
                found.add(str(ipaddress.IPv4Address(data)))
            offset += length
        return found


def _skip_name(reply: bytes, offset: int) -> int:
    """The offset after a name: labels end with a zero byte or a pointer to an earlier name."""
    while True:
        length = reply[offset]
        if length & 0xC0 == 0xC0:  # noqa: PLR2004
            return offset + 2
        if not length:
            return offset + 1
        offset += length + 1


def ask_resolver(server: str, name: str) -> set[str]:
    return DnsQuery(name).ask(server)


class Resolvers:
    """The resolvers of DOMAIN_RESOLVERS, asked one after another for the A records of a name."""

    def __init__(
        self,
        names: Sequence[str],
        system: Callable[[str], set[str]],
        public: Callable[[str, str], set[str]] | None = None,
    ) -> None:
        self._names = list(dict.fromkeys(name.strip() for name in names if name.strip()))
        self._system = system
        self._public = public or ask_resolver

    @classmethod
    def configured(cls, system: Callable[[str], set[str]]) -> "Resolvers":
        return cls(settings.DOMAIN_RESOLVERS, system)

    def ask(self, name: str) -> list[Answer]:
        return [self._answer(resolver, name) for resolver in self._names or [SYSTEM]]

    def _answer(self, resolver: str, name: str) -> Answer:
        if resolver == SYSTEM:
            return Answer(resolver, frozenset(self._system(name)))
        try:
            return Answer(resolver, frozenset(self._public(resolver, name)))
        except UnreachableResolverError:
            return Answer(resolver, reachable=False)
