"""Access keys through the API: owner, server, protocol, status and dates, within the token's zone.

A record holds no key material: not the private key or the profile, and not `public_key` either,
which for XRay is the client UUID, the access itself.
"""

from typing import Any

from django.db.models import QuerySet

from apps.api.errors import NotFoundError
from apps.keys.models import AccessKey
from apps.keys.services.zone import KeyZone
from apps.protocols.models import ProtocolInstance


class KeyRecords:
    def __init__(self, zone: KeyZone) -> None:
        self._zone = zone

    def rows(
        self, *, user: int | None = None, server: int | None = None, status: str | None = None
    ) -> QuerySet[AccessKey]:
        keys = self._zone.keys().order_by("pk")
        if user is not None:
            keys = keys.filter(user_id=user)
        if server is not None:
            ids = ProtocolInstance.objects.filter(server_id=server).values_list("pk", flat=True)
            keys = keys.filter(source__in=[ProtocolInstance.source_for(pk) for pk in ids])
        if status is not None:
            keys = keys.filter(status=status)
        return keys

    def get(self, pk: int) -> AccessKey:
        key = self._zone.keys().filter(pk=pk).first()
        if key is None:
            raise NotFoundError
        return key

    def records(self, keys: list[AccessKey]) -> list[dict[str, Any]]:
        instances = _instances({key.source for key in keys})
        return [_record(key, instances.get(key.source)) for key in keys]


def _instances(sources: set[str]) -> dict[str, tuple[int, str]]:
    """The server id and container of each instance named by `sources`."""
    ids = [pk for pk in map(ProtocolInstance.pk_of, sources) if pk is not None]
    rows = ProtocolInstance.objects.filter(pk__in=ids).values_list("pk", "server_id", "container")
    return {ProtocolInstance.source_for(pk): (server, container) for pk, server, container in rows}


def _record(key: AccessKey, instance: tuple[int, str] | None) -> dict[str, Any]:
    server_id, container = instance or (None, "")
    return {
        "id": key.pk,
        "name": key.name,
        "user_id": key.user_id,
        "server_id": server_id,
        "source": key.source,
        "protocol": container,
        "status": key.status,
        "imported": key.imported,
        "created_at": key.created_at,
        "first_viewed_at": key.first_viewed_at,
        "revoked_at": key.revoked_at,
    }
