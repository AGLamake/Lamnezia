"""What the panel knows of a user when they open a request, so the staff need not ask."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db.models import OuterRef, Subquery

from apps.accounts.models import User
from apps.guides.services.devices import DetectedDevice
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import instances
from apps.plans.models import UserLimits
from apps.traffic.models import TrafficCounter

_USER_AGENT_LENGTH = 500


def snapshot(user: User, user_agent: str) -> dict[str, Any]:
    """Plan, keys with their last handshake, and the browser, as flat JSON."""
    limits = UserLimits.objects.select_related("plan").filter(user=user).first()
    device = DetectedDevice.from_user_agent(user_agent).device
    return {
        "plan": limits.plan.name if limits and limits.plan else "",
        "keys": _keys(user),
        "user_agent": user_agent[:_USER_AGENT_LENGTH],
        "device": device.value if device else "",
    }


@dataclass(frozen=True)
class KeySeen:
    """A key of the user and when it was last seen online (`None`: never)."""

    name: str
    server: str
    status: str
    seen: datetime | None


def keys_seen(user: User) -> list[KeySeen]:
    """Keys in use, oldest first. XRay clients have no handshake, only traffic, so the later of
    the two says when a key was last seen.
    """
    counter = TrafficCounter.objects.filter(
        source=OuterRef("source"), public_key=OuterRef("public_key")
    )
    keys = (
        AccessKey.objects.filter(user=user)
        .exclude(status=KeyStatus.REVOKED)
        .annotate(
            handshake=Subquery(counter.values("latest_handshake")[:1]),
            active=Subquery(counter.values("active_at")[:1]),
        )
        .order_by("created_at")
    )
    titles = instances.titles()
    return [
        KeySeen(
            name=key.name,
            server=titles.get(key.source, key.source),
            status=key.status,
            seen=max(filter(None, (key.handshake, key.active)), default=None),
        )
        for key in keys
    ]


def _keys(user: User) -> list[dict[str, str]]:
    return [
        {
            "name": key.name,
            "server": key.server,
            "status": key.status,
            "handshake": key.seen.isoformat() if key.seen else "",
        }
        for key in keys_seen(user)
    ]
