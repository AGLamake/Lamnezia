import socket
from typing import Any

import pytest

from apps.notifications.services.addresses import AddressGuard, BlockedAddressError


def resolving_to(*addresses: str) -> Any:
    def resolve(host: str, port: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (a, 443)) for a in addresses]

    return resolve


def unresolvable(host: str, port: Any, **kwargs: Any) -> Any:
    raise socket.gaierror("no such name")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",
        "http://localhost:8000/hook",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://[::ffff:10.0.0.1]/",
    ],
)
def test_private_and_loopback_addresses_are_refused(url: str) -> None:
    with pytest.raises(BlockedAddressError):
        AddressGuard(allow_private=False).check(url)


def test_a_name_that_resolves_into_a_private_network_is_refused() -> None:
    guard = AddressGuard(allow_private=False, resolve=resolving_to("8.8.8.8", "fe80::1%eth0"))

    with pytest.raises(BlockedAddressError) as error:
        guard.check("https://hooks.example.org/x")

    assert (error.value.host, error.value.address) == ("hooks.example.org", "fe80::1")


def test_a_public_address_passes() -> None:
    AddressGuard(allow_private=False, resolve=resolving_to("8.8.8.8")).check("https://a.example/x")


def test_an_address_without_a_host_is_refused() -> None:
    with pytest.raises(BlockedAddressError):
        AddressGuard(allow_private=False).check("http:///path")


def test_a_name_that_does_not_resolve_passes() -> None:
    AddressGuard(allow_private=False, resolve=unresolvable).check("https://nowhere.invalid/")


def test_private_networks_pass_when_allowed() -> None:
    AddressGuard(allow_private=True).check("http://10.0.0.5/hook")
