"""Addresses of the machine the panel runs on, to keep a move from targeting that same machine."""

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

from django.conf import settings


def resolve(name: str) -> set[str]:
    """IP addresses a host name points to; an empty set when it does not resolve."""
    try:
        return {str(info[4][0]) for info in socket.getaddrinfo(name, None)}
    except (OSError, UnicodeError):
        return set()


class PanelMachine:
    """The panel's own host names and the IP addresses they resolve to (its external IP)."""

    def __init__(self, hosts: Iterable[str]) -> None:
        self._hosts = {name for name in map(_bare_host, hosts) if name}
        self._addresses: set[str] | None = None

    @classmethod
    def current(cls, request_host: str = "") -> "PanelMachine":
        """The panel's domain from SITE_URL and the host the admin opened the panel at."""
        return cls([urlsplit(settings.SITE_URL).hostname or "", request_host])

    def includes(self, host: str) -> bool:
        """Whether the host is this machine: the same name or a shared IP address."""
        name = _bare_host(host)
        if not name:
            return False
        if name in self._hosts:
            return True
        return bool(_addresses_of(name) & self.addresses)

    @property
    def addresses(self) -> set[str]:
        if self._addresses is None:
            self._addresses = set().union(*map(_addresses_of, self._hosts))
        return self._addresses


def _bare_host(host: str) -> str:
    """The host alone, lower-case: «Panel.Example.com.:443» → «panel.example.com»."""
    host = host.strip()
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        return (urlsplit(f"//{host}").hostname or "").rstrip(".")
    except ValueError:
        return ""


def _addresses_of(name: str) -> set[str]:
    try:
        return {str(ipaddress.ip_address(name))}
    except ValueError:
        return {str(ipaddress.ip_address(address.split("%")[0])) for address in resolve(name)}
