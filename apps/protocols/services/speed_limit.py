"""The speed limit of a whole protocol on its server: kept on the instance, applied by traffic.

The limit is a panel setting: saving it changes nothing on the server by itself. The speed task
of the traffic app starts the operation that rewrites the tc rules, right after the save.
"""

from django.db import transaction

from amnezia_core.containers import WG_CONTAINERS
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.protocols.models import ProtocolInstance
from apps.traffic.models import TrafficSource
from apps.traffic.tasks import apply_speed_limits

_FIELDS = ("download_limit", "upload_limit")


class ServerSpeedLimit:
    """The total download and upload limit of one WireGuard-family instance, in Mbit/s."""

    def __init__(self, instance: ProtocolInstance) -> None:
        self._instance = instance

    @property
    def applies(self) -> bool:
        """WireGuard family only, and not on a group clone: keys are shaped on the leading one."""
        if self._instance.container not in WG_CONTAINERS:
            return False
        member = getattr(self._instance, "group_member", None)
        return member is None or member.leading

    @property
    def upload_policed(self) -> bool:
        """The latest speed operation dropped upload over the limits: the host has no ifb."""
        return TrafficSource.objects.filter(
            source=self._instance.source, upload_policed=True
        ).exists()

    def save(
        self, download: int | None, upload: int | None, actor: User, ip: str | None = None
    ) -> bool:
        """Save the limits and have them applied; return whether they changed."""
        instance = self._instance
        if (instance.download_limit, instance.upload_limit) == (download, upload):
            return False
        instance.download_limit, instance.upload_limit = download, upload
        instance.save(update_fields=[*_FIELDS, "updated_at"])
        audit.log(
            "protocols.speed_limit",
            actor=actor,
            target=instance,
            data={"download": download, "upload": upload},
            ip=ip,
        )
        transaction.on_commit(apply_speed_limits.delay)
        return True
