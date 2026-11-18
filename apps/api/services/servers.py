"""Servers through the API: their state, latest load and protocols, within the token's zone.

SSH access, server keys and protocol parameters are never part of a record.
"""

from typing import Any

from django.db.models import QuerySet

from apps.accounts.services.zone import Zone
from apps.api.errors import NotFoundError
from apps.protocols.models import ProtocolInstance
from apps.servers.models import Server, ServerSample


class ServerRecords:
    def __init__(self, zone: Zone) -> None:
        self._zone = zone

    def rows(self) -> QuerySet[Server]:
        servers = self._zone.narrow(Server.objects.all(), server="pk")
        return servers.select_related("latest_sample").prefetch_related("instances").order_by("pk")

    def get(self, pk: int) -> Server:
        server = self.rows().filter(pk=pk).first()
        if server is None:
            raise NotFoundError
        return server

    @staticmethod
    def record(server: Server) -> dict[str, Any]:
        return {
            "id": server.pk,
            "name": server.name,
            "host": server.host,
            "location": server.location,
            "status": server.status,
            "online": server.online,
            "maintenance": server.maintenance,
            "paid_until": server.paid_until,
            "checked_at": server.checked_at,
            "last_seen_at": server.last_seen_at,
            "load": _load(server.latest_sample),
            "protocols": [_protocol(instance) for instance in server.instances.all()],
        }


def _load(sample: ServerSample | None) -> dict[str, Any] | None:
    if sample is None:
        return None
    return {
        "at": sample.at,
        "answered": sample.answered,
        "cpu_percent": sample.cpu_percent,
        "memory_percent": sample.memory_percent,
        "disk_percent": sample.disk_percent,
        "uptime_seconds": sample.uptime,
    }


def _protocol(instance: ProtocolInstance) -> dict[str, Any]:
    return {
        "source": instance.source,
        "container": instance.container,
        "title": instance.title,
        "port": instance.port,
        "removed": instance.removed,
    }
