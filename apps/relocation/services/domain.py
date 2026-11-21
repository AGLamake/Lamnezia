"""The background check that the panel domain's A record points to the new machine of a move.

The domain is looked up through every resolver of DOMAIN_RESOLVERS (services/resolvers.py); it
points to the new machine only when each resolver that answers leads there. The old copy tells
the staff once when the domain switches.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.relocation.models import DomainStatus, MoveState, PanelMove
from apps.relocation.services.resolvers import Answer, Resolvers
from apps.relocation.services.shutdown import request_stop


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
        """Check the latest move if it waits for the domain; the move with the new status.

        Once the domain points to the new machine, the old copy asks to stop (services/shutdown.py).
        """
        move = PanelMove.latest()
        if not self._domain or move is None or not (move.awaits_domain or move.should_stop):
            return None
        answers = Resolvers.configured(a_records).ask(self._domain)
        found = set().union(*(answer.addresses for answer in answers))
        was_pointed = move.domain_status == DomainStatus.POINTED
        move.domain_status = self._status(answers, self._target_addresses(move.target_host))
        move.domain_addresses = ", ".join(sorted(found))[:255]
        move.domain_answers = [answer.to_dict() for answer in answers]
        move.domain_checked_at = timezone.now()
        move.save(
            update_fields=[
                "domain_status",
                "domain_addresses",
                "domain_answers",
                "domain_checked_at",
            ]
        )
        if not was_pointed:
            self._tell_switched(move)
        request_stop(move)
        return move

    def _tell_switched(self, move: PanelMove) -> None:
        """Once per move, only from the old copy: the copy on the new machine checks too."""
        if move.state != MoveState.LEFT or move.domain_status != DomainStatus.POINTED:
            return
        url = settings.SITE_URL.rstrip("/") + reverse("relocation:progress", args=[move.pk])
        emit(
            Event.PANEL_DOMAIN_SWITCHED,
            {"domain": self._domain, "host": move.target_host, "url": url},
            key=f"{Event.PANEL_DOMAIN_SWITCHED}:{move.pk}",
        )

    @staticmethod
    def _target_addresses(host: str) -> set[str]:
        try:
            return {str(ipaddress.ip_address(host))}
        except ValueError:
            return a_records(host)

    @staticmethod
    def _status(answers: list[Answer], target: set[str]) -> DomainStatus:
        heard = [answer.addresses for answer in answers if answer.reachable]
        if not any(heard):
            return DomainStatus.UNRESOLVED
        # Every resolver that answers, with every A record, must lead to the new machine: users
        # behind a resolver that still gives the old machine reach the read-only copy.
        if target and all(found and found <= target for found in heard):
            return DomainStatus.POINTED
        return DomainStatus.ELSEWHERE
