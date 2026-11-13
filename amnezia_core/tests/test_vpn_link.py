import base64
import json
import struct
import zlib

import pytest

from amnezia_core.errors import VpnLinkError
from amnezia_core.qr import qr_png, qr_svg
from amnezia_core.vpn_link import (
    decode_vpn_link,
    encode_vpn_link,
    link_from_qr_payloads,
    qcompress,
    qr_payloads,
    quncompress,
)

CONFIG = {"hostName": "vpn.example.com", "containers": [{"container": "amnezia-awg2"}], "dns1": "я"}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_qcompress_has_big_endian_length_and_zlib_level_8() -> None:
    data = b"x" * 1000

    packed = qcompress(data)

    assert packed[:4] == struct.pack(">I", 1000)
    assert packed[4:] == zlib.compress(data, 8)
    assert quncompress(packed) == data


def test_link_is_base64url_without_padding_of_qt_json() -> None:
    link = encode_vpn_link(CONFIG)
    payload = link.removeprefix("vpn://")

    assert link.startswith("vpn://")
    assert "=" not in payload
    assert "+" not in payload
    assert "/" not in payload
    raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    assert quncompress(raw).decode() == (
        '{\n    "containers": [\n        {\n            "container": "amnezia-awg2"\n'
        '        }\n    ],\n    "dns1": "я",\n    "hostName": "vpn.example.com"\n}\n'
    )


def test_decode_then_encode_gives_same_link() -> None:
    link = encode_vpn_link(CONFIG)

    assert decode_vpn_link(link) == CONFIG
    assert encode_vpn_link(decode_vpn_link(link)) == link


def test_decode_accepts_uncompressed_payload_like_the_app() -> None:
    link = "vpn://" + b64url(json.dumps(CONFIG).encode())

    assert decode_vpn_link(f"  {link}\n") == CONFIG


@pytest.mark.parametrize(
    "link",
    [
        "vpn://a",
        "vpn://***",
        "vpn://" + b64url(b"not json"),
        "vpn://" + b64url(qcompress(b"[1, 2]")),
    ],
)
def test_decode_rejects_garbage(link: str) -> None:
    with pytest.raises(VpnLinkError):
        decode_vpn_link(link)


def test_quncompress_rejects_non_zlib() -> None:
    with pytest.raises(VpnLinkError):
        quncompress(b"\x00\x00\x00\x05hello")


def test_qr_series_round_trip_and_chunk_layout() -> None:
    # Random-looking content does not compress, so it needs several 850-byte chunks.
    config = {"blob": base64.b64encode(bytes(range(256)) * 12).decode()}
    link = encode_vpn_link(config)

    payloads = qr_payloads(link)
    first = base64.urlsafe_b64decode(payloads[0] + "==")

    assert len(payloads) > 1
    assert struct.unpack(">hBBI", first[:8]) == (1984, len(payloads), 0, 850)
    assert link_from_qr_payloads(list(reversed(payloads))) == link


def test_qr_series_rejects_missing_or_foreign_chunks() -> None:
    payloads = qr_payloads(encode_vpn_link({"blob": "x" * 5000 + str(list(range(900)))}))

    with pytest.raises(VpnLinkError):
        link_from_qr_payloads(payloads[:-1] if len(payloads) > 1 else [])
    with pytest.raises(VpnLinkError):
        link_from_qr_payloads([b64url(struct.pack(">hBBI", 7, 1, 0, 0))])
    with pytest.raises(VpnLinkError):
        link_from_qr_payloads([b64url(b"abc")])


def test_qr_series_rejects_oversized_config() -> None:
    link = "vpn://" + b64url(bytes(850 * 256))

    with pytest.raises(VpnLinkError):
        qr_payloads(link)


def test_qr_svg_renders_markup() -> None:
    svg = qr_svg("vpn://abc")

    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")


def test_qr_png_is_an_image() -> None:
    assert qr_png("vpn://abc").startswith(b"\x89PNG\r\n\x1a\n")
