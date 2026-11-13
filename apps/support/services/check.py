"""The check a "server does not work" request starts at once (DESIGN.md 6.7): does SSH answer, do
the Amnezia containers run, when were the user's keys and the server's clients last seen online.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Max
from django.utils.dateparse import parse_datetime

from apps.accounts.models import User
from apps.operations.models import Operation
from apps.operations.services.launcher import launch
from apps.protocols.models import ProtocolInstance
from apps.servers.models import Server
from apps.servers.services.connection import connect
from apps.servers.services.monitor import Connect, ServerMonitor
from apps.support.models import Conversation
from apps.traffic.models import TrafficCounter

CHECK_KIND = "support.check_server"


@dataclass(frozen=True)
class CheckReport:
    """`containers` are {"name", "state"} as `docker ps` reports them."""

    checked_at: str
    answered: bool
    containers: list[dict[str, str]]
    user_handshake: str
    server_handshake: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "CheckReport | None":
        return cls(**data) if data else None

    @property
    def stopped(self) -> list[str]:
        return [c["name"] for c in self.containers if c["state"] != "running"]

    @property
    def ok(self) -> bool:
        return self.answered and not self.stopped

    @property
    def checked(self) -> datetime | None:
        return parse_datetime(self.checked_at)

    @property
    def user_seen(self) -> datetime | None:
        return parse_datetime(self.user_handshake) if self.user_handshake else None

    @property
    def server_seen(self) -> datetime | None:
        return parse_datetime(self.server_handshake) if self.server_handshake else None


class RequestServerCheck:
    """Reads the server the way the monitor does and keeps the result on the request."""

    def __init__(self, connect: Connect = connect) -> None:
        self._monitor = ServerMonitor(timedelta(days=settings.MONITOR_HISTORY_DAYS), connect)

    def run(self, conversation: Conversation, server: Server) -> CheckReport:
        sample = self._monitor.poll(server)
        report = CheckReport(
            checked_at=sample.at.isoformat(),
            answered=sample.answered,
            containers=list(sample.containers),
            user_handshake=_last_seen(server, conversation.user),
            server_handshake=_last_seen(server),
        )
        conversation.server_check = asdict(report)
        conversation.save(update_fields=["server_check"])
        return report


def start_check(conversation: Conversation, user: User) -> Operation:
    assert conversation.server is not None
    operation = launch(
        CHECK_KIND,
        {"conversation_id": conversation.pk},
        user=user,
        subject=conversation.server.name,
    )
    conversation.check_operation = operation
    conversation.save(update_fields=["check_operation"])
    return operation


def _last_seen(server: Server, user: User | None = None) -> str:
    """The latest handshake or, for XRay clients that have none, the latest traffic."""
    sources = ProtocolInstance.sources_of(server)
    peers = TrafficCounter.objects.filter(source__in=sources)
    if user is not None:
        keys = user.access_keys.filter(source__in=sources).values("public_key")
        peers = peers.filter(public_key__in=keys)
    latest = peers.aggregate(handshake=Max("latest_handshake"), active=Max("active_at"))
    seen = max(filter(None, latest.values()), default=None)
    return seen.isoformat() if seen else ""
