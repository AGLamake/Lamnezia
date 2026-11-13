"""`vpn://` links and the app's multi-part QR codes.

A link is `vpn://` + base64url (no padding) of `qCompress(json)`: 4-byte big-endian length
of the JSON followed by a zlib stream (ExportController::generateVpnUrl upstream).
"""

import base64
import binascii
import json
import struct
import zlib
from math import ceil
from typing import Any

from amnezia_core.errors import VpnLinkError
from amnezia_core.qt_json import to_qt_json

VPN_SCHEME = "vpn://"
CONFIG_FORMAT_VERSION = 1
DEFAULT_DNS = ("1.1.1.1", "1.0.0.1")
_COMPRESSION_LEVEL = 8
_LENGTH_HEADER = struct.Struct(">I")

# qrCodeUtils::generateQrCodeImageSeries: QDataStream of magic, chunk count, chunk id, QByteArray.
_QR_MAGIC = 1984
_QR_CHUNK_SIZE = 850
_QR_HEADER = struct.Struct(">hBBI")
_QR_MAX_CHUNKS = 255


def connection_config(
    host: str,
    container: dict[str, Any],
    description: str = "",
    dns: tuple[str, str] = DEFAULT_DNS,
) -> dict[str, Any]:
    """Server JSON of ExportController::generateConnectionConfig: one container, no SSH data.

    `container` is `{"container": <name>, <protocol>: {...server settings, "last_config"}}`.
    """
    data = {
        "format_version": CONFIG_FORMAT_VERSION,
        "description": description,
        "hostName": host,
        "containers": [container],
        "defaultContainer": container["container"],
        "dns1": dns[0],
        "dns2": dns[1],
    }
    return {key: value for key, value in data.items() if value != ""}


def qcompress(data: bytes) -> bytes:
    """Qt `qCompress(data, 8)`."""
    return _LENGTH_HEADER.pack(len(data)) + zlib.compress(data, _COMPRESSION_LEVEL)


def quncompress(data: bytes) -> bytes:
    """Qt `qUncompress`; the length header is only a size hint, as in Qt."""
    try:
        return zlib.decompress(data[_LENGTH_HEADER.size :])
    except zlib.error as error:
        raise VpnLinkError("vpn:// payload is not qCompress data.") from error


def encode_vpn_link(config: dict[str, Any]) -> str:
    return VPN_SCHEME + _encode_base64url(qcompress(to_qt_json(config).encode("utf-8")))


def decode_vpn_link(link: str) -> dict[str, Any]:
    """Server JSON from a link. Like the app, also accepts uncompressed payloads."""
    payload = _decode_base64url(link.strip().removeprefix(VPN_SCHEME))
    try:
        text = quncompress(payload)
    except VpnLinkError:
        text = payload
    try:
        config = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VpnLinkError("vpn:// payload is not JSON.") from error
    if not isinstance(config, dict):
        raise VpnLinkError("vpn:// payload must be a JSON object.")
    return config


def qr_payloads(link: str) -> list[str]:
    """Texts for the QR series the app scans (one QR per 850 bytes of compressed config)."""
    data = _decode_base64url(link.strip().removeprefix(VPN_SCHEME))
    count = ceil(len(data) / _QR_CHUNK_SIZE)
    if count > _QR_MAX_CHUNKS:
        raise VpnLinkError("Config is too large for a QR code series.")
    payloads = []
    for index in range(count):
        chunk = data[index * _QR_CHUNK_SIZE : (index + 1) * _QR_CHUNK_SIZE]
        header = _QR_HEADER.pack(_QR_MAGIC, count, index, len(chunk))
        payloads.append(_encode_base64url(header + chunk))
    return payloads


def link_from_qr_payloads(payloads: list[str]) -> str:
    """Reassemble a link from scanned QR texts in any order."""
    chunks: dict[int, bytes] = {}
    count = 0
    for payload in payloads:
        raw = _decode_base64url(payload)
        if len(raw) < _QR_HEADER.size:
            raise VpnLinkError("QR payload is too short.")
        magic, count, index, size = _QR_HEADER.unpack_from(raw)
        if magic != _QR_MAGIC:
            raise VpnLinkError("QR payload is not an Amnezia config chunk.")
        chunks[index] = raw[_QR_HEADER.size : _QR_HEADER.size + size]
    if sorted(chunks) != list(range(count)):
        raise VpnLinkError("Some QR codes of the series are missing.")
    return VPN_SCHEME + _encode_base64url(b"".join(chunks[i] for i in range(count)))


def _encode_base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode_base64url(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as error:
        raise VpnLinkError("vpn:// payload is not base64url.") from error
