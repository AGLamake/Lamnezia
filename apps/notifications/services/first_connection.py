"""Telling the owner that a new key has connected for the first time (`key_connected`)."""

from collections.abc import Iterable
from datetime import datetime

from django.conf import settings
from django.db.models import Q
from django.urls import reverse

from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import UnknownInstanceError, instances
from apps.notifications.events import Event
from apps.notifications.services import delivery
from apps.traffic.models import TrafficCounter


class FirstConnections:
    """A key is connected once a reading after its issue saw it in use: a handshake or, for
    protocols without one, counters that grew — the data `OnlineRule` reads. In a server group
    the counters of every member count, as they name the key's own source in `key_source`.

    Each key is told once: `AccessKey.connected_at` marks it. A key reissued for the same owner,
    instance and device is marked silently when an earlier one of them has connected. Imported
    keys were in use before the panel and are never told.
    """

    def tell_all(self, now: datetime) -> int:
        """Mark the keys connected since the last run; returns how many were told."""
        keys = list(
            AccessKey.objects.filter(
                status=KeyStatus.ACTIVE, connected_at__isnull=True, imported=False
            ).select_related("user")
        )
        told = 0
        for key in self._connected(keys):
            key.connected_at = now
            key.save(update_fields=["connected_at"])
            if not self._reissued(key):
                self._tell(key)
                told += 1
        return told

    @staticmethod
    def _connected(keys: list[AccessKey]) -> Iterable[AccessKey]:
        if not keys:
            return []
        peers = Q()
        for key in keys:
            peers |= Q(key_source=key.source, public_key=key.public_key)
        used: dict[tuple[str, str], list[datetime]] = {}
        for counter in TrafficCounter.objects.filter(peers):
            moments = used.setdefault((counter.key_source, counter.public_key), [])
            moments += [t for t in (counter.latest_handshake, counter.active_at) if t]
        return [
            key
            for key in keys
            if any(t > key.created_at for t in used.get((key.source, key.public_key), []))
        ]

    @staticmethod
    def _reissued(key: AccessKey) -> bool:
        """An earlier key of the same owner, instance and device has connected already."""
        return (
            AccessKey.objects.filter(
                user=key.user, source=key.source, name=key.name, connected_at__isnull=False
            )
            .exclude(pk=key.pk)
            .exists()
        )

    @staticmethod
    def _tell(key: AccessKey) -> None:
        try:
            instance = instances.get(key.source)
        except UnknownInstanceError:
            server, server_id = key.source, None
        else:
            server, server_id = instance.title, instance.server_id
        delivery.emit(
            Event.KEY_CONNECTED,
            {
                "user": key.user.username if key.user else "",
                "key": key.name,
                "key_id": key.pk,
                "server": server,
                "server_id": server_id,
                "url": settings.SITE_URL.rstrip("/") + reverse("portal:key", args=[key.pk]),
            },
            key=f"key_connected:{key.pk}",
            user=key.user,
        )
