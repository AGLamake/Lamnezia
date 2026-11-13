"""A minimal STUN responder (RFC 5389 Binding) for checking UDP reachability from browsers.

The panel uploads this file to a server and runs it in the `lamnezia-probe` container; the
panel also imports it to build and read the same messages. Standard library only, no logging:
the responder keeps nothing about who asked.

Usage: python stun_responder.py <port>
"""

import binascii
import ipaddress
import socket
import struct
import sys

MAGIC_COOKIE = 0x2112A442
BINDING_REQUEST = 0x0001
BINDING_SUCCESS = 0x0101
XOR_MAPPED_ADDRESS = 0x0020
FINGERPRINT = 0x8028
_FINGERPRINT_XOR = 0x5354554E
_HEADER = struct.Struct("!HHI12s")
_IPV4, _IPV6 = 0x01, 0x02
_MAX_REQUEST = 548


def binding_request(transaction_id: bytes) -> bytes:
    return _HEADER.pack(BINDING_REQUEST, 0, MAGIC_COOKIE, transaction_id)


def binding_response(request: bytes, host: str, port: int) -> bytes | None:
    """The answer to a Binding request from host:port, or None for anything else."""
    if not _HEADER.size <= len(request) <= _MAX_REQUEST:
        return None
    kind, length, cookie, transaction_id = _HEADER.unpack_from(request)
    if kind != BINDING_REQUEST or cookie != MAGIC_COOKIE or length != len(request) - _HEADER.size:
        return None
    mapped = _xor_mapped_address(ipaddress.ip_address(host), port, transaction_id)
    return _with_fingerprint(
        BINDING_SUCCESS, transaction_id, _attribute(XOR_MAPPED_ADDRESS, mapped)
    )


def mapped_address(response: bytes, transaction_id: bytes) -> tuple[str, int] | None:
    """The address a Binding success response reports, or None if it is not ours."""
    if len(response) < _HEADER.size:
        return None
    kind, length, cookie, received_id = _HEADER.unpack_from(response)
    if kind != BINDING_SUCCESS or cookie != MAGIC_COOKIE or received_id != transaction_id:
        return None
    offset = _HEADER.size
    end = min(len(response), offset + length)
    while offset + 4 <= end:
        attribute, size = struct.unpack_from("!HH", response, offset)
        value = response[offset + 4 : offset + 4 + size]
        if attribute == XOR_MAPPED_ADDRESS and len(value) in (8, 20):
            return _read_xor_mapped_address(value, transaction_id)
        offset += 4 + (size + 3) // 4 * 4
    return None


def serve(port: int) -> None:
    sock = _bind(port)
    while True:
        try:
            request, address = sock.recvfrom(_MAX_REQUEST + 1)
            response = binding_response(request, _plain_host(address[0]), address[1])
            if response is not None:
                sock.sendto(response, address)
        except (OSError, ValueError):
            continue


def _xor_mapped_address(
    host: ipaddress.IPv4Address | ipaddress.IPv6Address, port: int, transaction_id: bytes
) -> bytes:
    family = _IPV4 if isinstance(host, ipaddress.IPv4Address) else _IPV6
    key = struct.pack("!I", MAGIC_COOKIE) + (transaction_id if family == _IPV6 else b"")
    address = bytes(a ^ b for a, b in zip(host.packed, key, strict=True))
    return struct.pack("!BBH", 0, family, port ^ (MAGIC_COOKIE >> 16)) + address


def _read_xor_mapped_address(value: bytes, transaction_id: bytes) -> tuple[str, int] | None:
    _, family, xport = struct.unpack_from("!BBH", value)
    key = struct.pack("!I", MAGIC_COOKIE) + (transaction_id if family == _IPV6 else b"")
    if len(value) - 4 != len(key):
        return None
    packed = bytes(a ^ b for a, b in zip(value[4:], key, strict=True))
    return str(ipaddress.ip_address(packed)), xport ^ (MAGIC_COOKIE >> 16)


def _attribute(kind: int, value: bytes) -> bytes:
    padding = b"\0" * (-len(value) % 4)
    return struct.pack("!HH", kind, len(value)) + value + padding


def _with_fingerprint(kind: int, transaction_id: bytes, attributes: bytes) -> bytes:
    """Clients may check FINGERPRINT; its length is counted in the header before the CRC."""
    header = _HEADER.pack(kind, len(attributes) + 8, MAGIC_COOKIE, transaction_id)
    crc = binascii.crc32(header + attributes) ^ _FINGERPRINT_XOR
    return header + attributes + _attribute(FINGERPRINT, struct.pack("!I", crc))


def _bind(port: int) -> socket.socket:
    """Both IPv4 and IPv6 when the host has IPv6, IPv4 only otherwise."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(("::", port))
    except OSError:
        if sock is not None:
            sock.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", port))
    return sock


def _plain_host(host: str) -> str:
    """`::ffff:1.2.3.4` from a dual-stack socket is the IPv4 client 1.2.3.4."""
    address = ipaddress.ip_address(host.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return str(address.ipv4_mapped)
    return str(address)


if __name__ == "__main__":
    serve(int(sys.argv[1]))
