"""Templates of the AmneziaWG special junk packet I1 that copy the start of real protocols.

The Amnezia app gives every server the same I1, a DNS answer for icloud.com with a Yandex
address: a censor learns it once. A template here starts like a real STUN, QUIC, DNS or SIP
packet, written by the RFC, with the fields a real client changes on every packet left to the
tags `<r N>`, `<rc N>` and `<rd N>` and the per-session fields drawn once per server.

Only the tags both amneziawg-go and the kernel module understand are used: `<b 0x..>`,
`<r N>`, `<rc N>`, `<rd N>` and `<t>`. `check_special_junk` holds an administrator's own value
to the same tags and to MAX_PACKET bytes.
"""

import random
import re
import secrets
from collections.abc import Callable
from enum import StrEnum
from ipaddress import IPv4Address
from typing import Self

# The I1 of upstream AwgInstaller (defaultSpecialJunk1): the same on every server of the app.
APP_I1 = (
    "<r 2><b 0x858000010001000000000669636c6f756403636f6d0000010001c00c000100010000105a"
    "00044d583737>"
)
# The packet must leave room for the IP and UDP headers within a 1500-byte link.
MAX_PACKET = 1400
# A QUIC client pads its Initial packet to at least 1200 bytes (RFC 9000, 14.1).
QUIC_MIN_INITIAL = 1200
_QUIC_PADDING_SPREAD = 50
# The size of `<t>` and `<c>`: a 32-bit time and counter.
_WORD = 4

_TAG = re.compile(r"<(?P<name>b|r|rc|rd|t|c)(?: (?P<arg>0x[0-9a-fA-F]+|\d+))?>")
_PORTABLE = re.compile(r"(?:<(?:b 0x(?:[0-9a-fA-F]{2})+|r \d+|rc \d+|rd \d+|t)>)+")


class Masking(StrEnum):
    """A template of I1; `ICLOUD` is the packet of the app with a real Apple address."""

    STUN = "stun"
    QUIC = "quic"
    DNS = "dns"
    SIP = "sip"
    ICLOUD = "icloud"

    @classmethod
    def pick(cls, rng: random.Random | None = None) -> Self:
        return (rng or secrets.SystemRandom()).choice(list(cls))

    @classmethod
    def named(cls, name: str) -> Self | None:
        """The template of that name; None for any other value, which stands for a random one."""
        return cls(name) if name in cls._value2member_map_ else None

    def packet(self, rng: random.Random | None = None) -> str:
        """A new I1 of this template; the per-server fields are drawn from `rng`."""
        return _BUILDERS[self](rng or secrets.SystemRandom())


class JunkProblem(StrEnum):
    NOT_PORTABLE = "not_portable"
    TOO_LONG = "too_long"


def check_special_junk(value: str) -> JunkProblem | None:
    """What keeps an I1-I5 value from working on every AmneziaWG server, if anything."""
    if _PORTABLE.fullmatch(value) is None:
        return JunkProblem.NOT_PORTABLE
    if packet_size(value) > MAX_PACKET:
        return JunkProblem.TOO_LONG
    return None


def packet_size(value: str) -> int:
    """Bytes of the packet a tagged value makes."""
    size = 0
    for tag in _TAG.finditer(value):
        name, arg = tag["name"], tag["arg"]
        if name == "b":
            size += (len(arg) - 2) // 2
        elif name in ("t", "c"):
            size += _WORD
        else:
            size += int(arg)
    return size


def is_app_masking(value: str) -> bool:
    """`value` is the I1 every server of the Amnezia app sends."""
    return value.strip() == APP_I1


class _Packet:
    """Bytes and tags in order; adjacent bytes go into one `<b 0x..>`."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._bytes = bytearray()

    def add(self, data: bytes) -> Self:
        self._bytes += data
        return self

    def text(self, value: str) -> Self:
        return self.add(value.encode("ascii"))

    def tag(self, name: str, size: int) -> Self:
        self._flush()
        self._parts.append(f"<{name} {size}>")
        return self

    def build(self) -> str:
        self._flush()
        return "".join(self._parts)

    def _flush(self) -> None:
        if self._bytes:
            self._parts.append(f"<b 0x{self._bytes.hex()}>")
            self._bytes.clear()


def _u16(value: int) -> bytes:
    return value.to_bytes(2, "big")


def _u32(value: int) -> bytes:
    return value.to_bytes(4, "big")


def _name(domain: str) -> bytes:
    """A DNS name in labels (RFC 1035, 3.1)."""
    labels = (label.encode("ascii") for label in domain.split("."))
    return b"".join(bytes([len(label)]) + label for label in labels) + b"\0"


def _letters(rng: random.Random, size: int) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(rng.choice(alphabet) for _ in range(size))


def _digits(rng: random.Random, size: int) -> str:
    return "".join(rng.choice("0123456789") for _ in range(size))


def _lan_address(rng: random.Random) -> str:
    return f"192.168.{rng.randint(0, 31)}.{rng.randint(2, 254)}"


# STUN (RFC 8489) as a WebRTC ICE connectivity check sends it (RFC 8445, 7.1.1).
_STUN_BINDING_REQUEST = 0x0001
_STUN_MAGIC_COOKIE = 0x2112A442
_STUN_USERNAME = 0x0006
_STUN_PRIORITY = 0x0024
_STUN_ICE_CONTROLLING = 0x802A
_STUN_MESSAGE_INTEGRITY = 0x0008
_STUN_FINGERPRINT = 0x8028
_STUN_UFRAG = 4
_STUN_TIE_BREAKER = 8
_STUN_HMAC = 20
_STUN_CRC = 4
_STUN_TRANSACTION = 12
# A host candidate of UDP: type preference 126 (RFC 8445, 5.1.2.1).
_STUN_HOST_PRIORITY = 126 << 24


def _stun(rng: random.Random) -> str:
    username = f"{_letters(rng, _STUN_UFRAG)}:{_letters(rng, _STUN_UFRAG)}".encode("ascii")
    padded = username + b"\0" * (-len(username) % 4)
    priority = _STUN_HOST_PRIORITY | rng.randint(0, 0xFFFF) << 8 | 0xFF
    attributes = (
        (4 + len(padded)) + (4 + 4) + (4 + _STUN_TIE_BREAKER) + (4 + _STUN_HMAC) + (4 + _STUN_CRC)
    )
    return (
        _Packet()
        .add(_u16(_STUN_BINDING_REQUEST) + _u16(attributes) + _u32(_STUN_MAGIC_COOKIE))
        .tag("r", _STUN_TRANSACTION)
        .add(_u16(_STUN_USERNAME) + _u16(len(username)) + padded)
        .add(_u16(_STUN_PRIORITY) + _u16(4) + _u32(priority))
        .add(_u16(_STUN_ICE_CONTROLLING) + _u16(_STUN_TIE_BREAKER))
        .tag("r", _STUN_TIE_BREAKER)
        .add(_u16(_STUN_MESSAGE_INTEGRITY) + _u16(_STUN_HMAC))
        .tag("r", _STUN_HMAC)
        .add(_u16(_STUN_FINGERPRINT) + _u16(_STUN_CRC))
        .tag("r", _STUN_CRC)
        .build()
    )


# QUIC version 1 Initial packet (RFC 9000, 17.2.2): header protection hides the low bits of the
# first byte and the packet number, the payload is encrypted, so both are random bytes.
_QUIC_LONG_INITIAL = 0xC0
_QUIC_VERSION_1 = 0x00000001
_QUIC_CONNECTION_ID = 8
_QUIC_PACKET_NUMBER = 4
_QUIC_TWO_BYTE_LENGTH = 0x4000


def _quic(rng: random.Random) -> str:
    size = QUIC_MIN_INITIAL + rng.randint(0, _QUIC_PADDING_SPREAD)
    header = (
        bytes([_QUIC_LONG_INITIAL | rng.randint(0, 0x0F)])
        + _u32(_QUIC_VERSION_1)
        + bytes([_QUIC_CONNECTION_ID])
    )
    # Header so far, both connection IDs with the length of the second, token length, Length.
    fixed = len(header) + _QUIC_CONNECTION_ID + 1 + _QUIC_CONNECTION_ID + 1 + 2
    remainder = size - fixed
    return (
        _Packet()
        .add(header)
        .tag("r", _QUIC_CONNECTION_ID)
        .add(bytes([_QUIC_CONNECTION_ID]))
        .tag("r", _QUIC_CONNECTION_ID)
        .add(b"\0" + _u16(_QUIC_TWO_BYTE_LENGTH | remainder))
        .tag("r", remainder)
        .build()
    )


# DNS (RFC 1035) query for A, AAAA or HTTPS with an EDNS0 cookie (RFC 6891, RFC 7873).
_DNS_QUERY_FLAGS = 0x0100
_DNS_RESPONSE_FLAGS = 0x8180
_DNS_CLASS_IN = 1
_DNS_TYPE_A = 1
_DNS_QUERY_TYPES = (_DNS_TYPE_A, 28, 65)
_DNS_TYPE_OPT = 41
_DNS_UDP_PAYLOAD = 1232
_DNS_OPTION_COOKIE = 10
_DNS_CLIENT_COOKIE = 8
_DNS_ID = 2
_DNS_QUERY_DOMAINS = (
    "www.google.com",
    "ya.ru",
    "www.youtube.com",
    "vk.com",
    "www.wikipedia.org",
    "mail.ru",
    "www.apple.com",
    "www.microsoft.com",
)


def _dns(rng: random.Random) -> str:
    question = (
        _name(rng.choice(_DNS_QUERY_DOMAINS))
        + _u16(rng.choice(_DNS_QUERY_TYPES))
        + _u16(_DNS_CLASS_IN)
    )
    option = _u16(_DNS_OPTION_COOKIE) + _u16(_DNS_CLIENT_COOKIE)
    opt = b"\0" + _u16(_DNS_TYPE_OPT) + _u16(_DNS_UDP_PAYLOAD) + _u32(0)
    opt += _u16(len(option) + _DNS_CLIENT_COOKIE) + option
    return (
        _Packet()
        .tag("r", _DNS_ID)
        .add(_u16(_DNS_QUERY_FLAGS) + _u16(1) + _u16(0) + _u16(0) + _u16(1))
        .add(question + opt)
        .tag("r", _DNS_CLIENT_COOKIE)
        .build()
    )


# The answer of the app's I1 with an address of Apple, the owner of 17.0.0.0/8.
_ICLOUD_ADDRESSES = ("17.253.144.10", "17.253.144.12", "17.57.146.52", "17.57.146.54")
_ICLOUD_TTL = (60, 3600)


def _icloud(rng: random.Random) -> str:
    address = IPv4Address(rng.choice(_ICLOUD_ADDRESSES)).packed
    question = _name("icloud.com") + _u16(_DNS_TYPE_A) + _u16(_DNS_CLASS_IN)
    # The answer names the question by a pointer to offset 12 (RFC 1035, 4.1.4).
    answer = _u16(0xC00C) + _u16(_DNS_TYPE_A) + _u16(_DNS_CLASS_IN)
    answer += _u32(rng.randint(*_ICLOUD_TTL)) + _u16(len(address)) + address
    return (
        _Packet()
        .tag("r", _DNS_ID)
        .add(_u16(_DNS_RESPONSE_FLAGS) + _u16(1) + _u16(1) + _u16(0) + _u16(0))
        .add(question + answer)
        .build()
    )


# SIP INVITE (RFC 3261, 8.1.1) over UDP without a body; branch, tag and Call-ID change per call.
_SIP_PORT = 5060
_SIP_BRANCH = 16
_SIP_TAG = 10
_SIP_CALL_ID = 24
_SIP_NUMBER = 6


def _sip(rng: random.Random) -> str:
    caller, callee = _digits(rng, _SIP_NUMBER), _digits(rng, _SIP_NUMBER)
    local = _lan_address(rng)
    domain = f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
    return (
        _Packet()
        .text(f"INVITE sip:{callee}@{domain} SIP/2.0\r\n")
        .text(f"Via: SIP/2.0/UDP {local}:{_SIP_PORT};rport;branch=z9hG4bK")
        .tag("rc", _SIP_BRANCH)
        .text(f"\r\nMax-Forwards: 70\r\nFrom: <sip:{caller}@{domain}>;tag=")
        .tag("rd", _SIP_TAG)
        .text(f"\r\nTo: <sip:{callee}@{domain}>\r\nCall-ID: ")
        .tag("rc", _SIP_CALL_ID)
        .text(f"\r\nCSeq: 1 INVITE\r\nContact: <sip:{caller}@{local}:{_SIP_PORT}>\r\n")
        .text("Allow: INVITE, ACK, CANCEL, BYE, OPTIONS\r\nContent-Length: 0\r\n\r\n")
        .build()
    )


_BUILDERS: dict[Masking, Callable[[random.Random], str]] = {
    Masking.STUN: _stun,
    Masking.QUIC: _quic,
    Masking.DNS: _dns,
    Masking.SIP: _sip,
    Masking.ICLOUD: _icloud,
}
