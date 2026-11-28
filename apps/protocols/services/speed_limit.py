"""The speed limit of a whole protocol on its server: kept on the instance, applied by traffic.

The limit is a panel setting: saving it changes nothing on the server by itself. The speed task
of the traffic app starts the operation that rewrites the tc rules, right after the save.
`ShapingState` tells the card when the latest reading found applied limits gone from a container.
"""

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from amnezia_core.containers import WG_CONTAINERS
from amnezia_core.xray import XRAY_CONTAINER
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation
from apps.protocols.models import ProtocolInstance
from apps.traffic.models import Shaping, TrafficSource
from apps.traffic.services.speed import SPEED_APPLY
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


# The nft file and table of XRay stand where WireGuard has the script and the tc rules.
_XRAY_SHAPING = {
    Shaping.MISSING: _("limits file missing"),
    Shaping.LOST: _("nftables table missing"),
}


class ShapingState:
    """What the latest reading found of the speed limits applied to one instance."""

    def __init__(self, instance: ProtocolInstance) -> None:
        self._instance = instance

    def lost(self) -> str:
        """Why the limits are not in the container, or empty: they are in place, the reading
        did not look, or the latest speed operation applied no limits at all."""
        source = self._instance.source
        shaping = (
            TrafficSource.objects.filter(source=source).values_list("shaping", flat=True).first()
        )
        if not shaping or shaping == Shaping.OK or not self._limits_applied():
            return ""
        if self._instance.container == XRAY_CONTAINER and shaping in _XRAY_SHAPING:
            return str(_XRAY_SHAPING[Shaping(shaping)])
        return str(Shaping(shaping).label)

    def _limits_applied(self) -> bool:
        latest = (
            Operation.objects.filter(kind=SPEED_APPLY, params__source=self._instance.source)
            .order_by("-created_at", "-pk")
            .first()
        )
        return latest is not None and bool(latest.params.get("rules") or latest.params.get("total"))
