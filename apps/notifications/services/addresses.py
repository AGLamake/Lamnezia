"""Where notifications may be sent: by default only to public addresses, not into private networks.

A webhook to 127.0.0.1, 10.0.0.0/8 or 169.254.169.254 would let whoever sets up the channel reach
services that only the panel can see. The panel settings can allow it for a receiver in one's own
network.
"""

import ipaddress
import socket
from collections.abc import Callable, Sequence
from typing import Any, Self
from urllib.parse import urlsplit

from apps.core.services.site_settings import cached_settings
from apps.notifications.services.pinned import PinnedAddresses

Resolver = Callable[..., Sequence[tuple[Any, ...]]]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class BlockedAddressError(ValueError):
    """The address is in a private, loopback or other non-public network."""

    def __init__(self, host: str, address: str) -> None:
        super().__init__(f"{host or 'no host'} → {address}: not a public address")
        self.host = host
        self.address = address


class UnresolvedHostError(ValueError):
    """The name does not resolve, so there is no checked address to send to."""

    def __init__(self, host: str) -> None:
        super().__init__(f"{host}: the name does not resolve")
        self.host = host


class AddressGuard:
    """Refuses URLs whose host is, or resolves to, a non-public address.

    `check` lets a host that does not resolve pass: a form may save it, and every send checks
    again. A send uses `pin`: the request goes only to the addresses checked, so a name that
    changes its address right after the check (DNS rebinding) cannot lead into a private network.
    """

    def __init__(self, *, allow_private: bool, resolve: Resolver | None = None) -> None:
        self._allow_private = allow_private
        self._resolve = resolve or socket.getaddrinfo

    @classmethod
    def from_settings(cls) -> Self:
        return cls(allow_private=cached_settings.get().webhooks_private_networks)

    def check(self, url: str) -> None:
        """Raise BlockedAddressError if the URL leads into a private network."""
        if self._allow_private:
            return
        host, port = self._host(url)
        self._refuse_private(host, self._addresses(host, port))

    def pin(self, url: str) -> PinnedAddresses | None:
        """The checked addresses the request must go to; None when private networks are allowed.

        Raises BlockedAddressError for a non-public address and UnresolvedHostError for a name
        that does not resolve.
        """
        if self._allow_private:
            return None
        host, port = self._host(url)
        addresses = self._addresses(host, port)
        if not addresses:
            raise UnresolvedHostError(host)
        self._refuse_private(host, addresses)
        return PinnedAddresses([str(address) for address in addresses])

    @staticmethod
    def _host(url: str) -> tuple[str, int | None]:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if not host:
            raise BlockedAddressError(host, "-")
        return host, parts.port

    @staticmethod
    def _refuse_private(host: str, addresses: list[IPAddress]) -> None:
        for address in addresses:
            if not address.is_global:
                raise BlockedAddressError(host, str(address))

    def _addresses(self, host: str, port: int | None) -> list[IPAddress]:
        try:
            infos = self._resolve(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            return []
        found: list[IPAddress] = []
        for info in infos:
            address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            found.append(address)
        return found
