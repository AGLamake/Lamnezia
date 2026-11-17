import socket
from typing import Any

import pytest

from apps.notifications.services.addresses import (
    AddressGuard,
    BlockedAddressError,
    UnresolvedHostError,
)


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


class TestPin:
    def test_a_send_is_pinned_to_the_checked_addresses(self) -> None:
        guard = AddressGuard(allow_private=False, resolve=resolving_to("8.8.8.8", "::ffff:1.1.1.1"))

        pinned = guard.pin("https://hooks.example.org/x")

        assert pinned is not None
        assert pinned.addresses == ("8.8.8.8", "1.1.1.1")

    def test_a_private_address_is_not_pinned(self) -> None:
        guard = AddressGuard(allow_private=False, resolve=resolving_to("8.8.8.8", "10.0.0.1"))

        with pytest.raises(BlockedAddressError):
            guard.pin("https://hooks.example.org/x")

    def test_a_name_that_does_not_resolve_cannot_be_sent_to(self) -> None:
        guard = AddressGuard(allow_private=False, resolve=unresolvable)

        with pytest.raises(UnresolvedHostError):
            guard.pin("https://nowhere.invalid/")

    def test_nothing_is_pinned_when_private_networks_are_allowed(self) -> None:
        assert AddressGuard(allow_private=True).pin("http://10.0.0.5/hook") is None
