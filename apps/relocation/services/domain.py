"""The background check that the panel domain's A record points to the new machine of a move."""

import ipaddress
import socket
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone

from apps.relocation.models import DomainStatus, PanelMove


def a_records(name: str) -> set[str]:
    """IPv4 addresses (A records) a host name points to; an empty set when it does not resolve."""
    try:
        infos = socket.getaddrinfo(name, None, family=socket.AF_INET)
    except (OSError, UnicodeError):
        return set()
    return {str(info[4][0]) for info in infos}


def panel_domain() -> str:
    """The panel's domain from SITE_URL; empty when the panel is opened by an IP address."""
    host = (urlsplit(settings.SITE_URL).hostname or "").rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    return ""


class DomainCheck:
    """Compares the A records of the panel domain with the addresses of the move's new machine."""

    def __init__(self, domain: str) -> None:
        self._domain = domain

    @classmethod
    def current(cls) -> "DomainCheck":
        return cls(panel_domain())

    def run(self) -> PanelMove | None:
        """Check the latest move if it waits for the domain; the move with the new status."""
        move = PanelMove.latest()
        if not self._domain or move is None or not move.awaits_domain:
            return None
        found = a_records(self._domain)
        move.domain_status = self._status(found, self._target_addresses(move.target_host))
        move.domain_addresses = ", ".join(sorted(found))[:255]
        move.domain_checked_at = timezone.now()
        move.save(update_fields=["domain_status", "domain_addresses", "domain_checked_at"])
        return move

    @staticmethod
    def _target_addresses(host: str) -> set[str]:
        try:
            return {str(ipaddress.ip_address(host))}
        except ValueError:
            return a_records(host)

    @staticmethod
    def _status(found: set[str], target: set[str]) -> DomainStatus:
        if not found:
            return DomainStatus.UNRESOLVED
        # Every A record must lead to the new machine: with one left on the old machine part of
        # the users still reach the read-only copy.
        if target and found <= target:
            return DomainStatus.POINTED
        return DomainStatus.ELSEWHERE
