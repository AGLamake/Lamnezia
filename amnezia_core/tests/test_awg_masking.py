"""I1 templates: portable tags, sizes, the real protocol headers and the fields that vary."""

import random
import re

import pytest

from amnezia_core.awg_masking import (
    APP_I1,
    MAX_PACKET,
    QUIC_MIN_INITIAL,
    JunkProblem,
    Masking,
    check_special_junk,
    is_app_masking,
    packet_size,
)

_TAG = re.compile(r"<(b|r|rc|rd|t|c)(?: (0x[0-9a-fA-F]+|\d+))?>")


def expand(value: str) -> bytes:
    """The packet a value makes, with zeros for the random and the time tags."""
    data = bytearray()
    for name, arg in _TAG.findall(value):
        if name == "b":
            data += bytes.fromhex(arg[2:])
        elif name in ("t", "c"):
            data += bytes(4)
        else:
            data += bytes(int(arg))
    return bytes(data)


@pytest.mark.parametrize("masking", list(Masking))
def test_every_template_is_portable_and_fits(masking: Masking) -> None:
    for seed in range(20):
        value = masking.packet(random.Random(seed))

        assert check_special_junk(value) is None
        assert packet_size(value) == len(expand(value)) <= MAX_PACKET


@pytest.mark.parametrize("masking", list(Masking))
def test_two_servers_get_different_packets(masking: Masking) -> None:
    values = {masking.packet(random.Random(seed)) for seed in range(10)}

    assert len(values) > 1


@pytest.mark.parametrize("masking", list(Masking))
def test_every_template_has_fields_that_change_per_packet(masking: Masking) -> None:
    assert re.search(r"<r[cd]? \d+>", masking.packet(random.Random(1)))


def test_stun_is_a_binding_request_with_the_right_length() -> None:
    packet = expand(Masking.STUN.packet(random.Random(1)))

    assert packet[:2] == b"\x00\x01"
    assert int.from_bytes(packet[2:4]) == len(packet) - 20
    assert packet[4:8] == bytes.fromhex("2112a442")


def test_quic_is_an_initial_packet_of_at_least_1200_bytes() -> None:
    for seed in range(20):
        packet = expand(Masking.QUIC.packet(random.Random(seed)))

        assert packet[0] & 0xF0 == 0xC0
        assert packet[1:5] == b"\x00\x00\x00\x01"
        assert len(packet) >= QUIC_MIN_INITIAL
        length = int.from_bytes(packet[24:26]) & 0x3FFF
        assert 26 + length == len(packet)


def test_dns_query_has_one_question_and_an_edns0_record() -> None:
    packet = expand(Masking.DNS.packet(random.Random(1)))

    assert packet[2:12] == bytes.fromhex("01000001000000000001")
    assert b"\x00\x00\x29" in packet


def test_icloud_answer_has_an_apple_address() -> None:
    packet = expand(Masking.ICLOUD.packet(random.Random(1)))

    assert b"\x06icloud\x03com\x00" in packet
    assert packet[-4] == 17
    assert not is_app_masking(Masking.ICLOUD.packet(random.Random(1)))


def test_sip_is_an_invite() -> None:
    packet = expand(Masking.SIP.packet(random.Random(1)))

    assert packet.startswith(b"INVITE sip:")
    assert packet.endswith(b"Content-Length: 0\r\n\r\n")


def test_random_choice_covers_the_templates() -> None:
    rng = random.Random(1)

    assert {Masking.pick(rng) for _ in range(100)} == set(Masking)


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("<b 0x0102><r 4><rc 3><rd 2><t>", None),
        ("<b 0x01><c>", JunkProblem.NOT_PORTABLE),
        ("<b 0x123>", JunkProblem.NOT_PORTABLE),
        ("plain", JunkProblem.NOT_PORTABLE),
        ("<r 1400>", None),
        ("<r 1397><t>", JunkProblem.TOO_LONG),
    ],
)
def test_check_special_junk(value: str, problem: JunkProblem | None) -> None:
    assert check_special_junk(value) is problem


def test_the_app_packet_is_recognised() -> None:
    assert is_app_masking(f" {APP_I1} ")
    assert check_special_junk(APP_I1) is None
