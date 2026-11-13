from ipaddress import IPv4Address, IPv4Interface

import pytest

from amnezia_core.errors import SubnetExhaustedError
from amnezia_core.ip_pool import IpPool

SERVER = IPv4Interface("10.8.1.0/24")


def ips(*last_octets: int) -> list[IPv4Address]:
    return [IPv4Address(f"10.8.1.{octet}") for octet in last_octets]


def test_first_client_gets_first_host() -> None:
    assert IpPool(SERVER, []).next_address() == IPv4Address("10.8.1.1")


def test_takes_address_after_highest_like_the_app() -> None:
    # A gap at .2 stays free: the app would pick last+1 and must not collide with us.
    assert IpPool(SERVER, ips(1, 3, 4)).next_address() == IPv4Address("10.8.1.5")


def test_reuses_gaps_when_top_is_taken() -> None:
    assert IpPool(SERVER, ips(1, 254)).next_address() == IPv4Address("10.8.1.2")


def test_skips_server_address() -> None:
    server = IPv4Interface("10.8.1.1/24")

    assert IpPool(server, []).next_address() == IPv4Address("10.8.1.2")


def test_skips_dot_zero_and_dot_255_in_wide_subnets() -> None:
    server = IPv4Interface("10.8.0.0/16")
    used = [IPv4Address("10.8.0.254")]

    assert IpPool(server, used).next_address() == IPv4Address("10.8.1.1")


def test_raises_when_subnet_is_full() -> None:
    server = IPv4Interface("10.8.1.0/30")

    with pytest.raises(SubnetExhaustedError):
        IpPool(server, ips(1, 2)).next_address()
