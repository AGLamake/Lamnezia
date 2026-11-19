"""Template tags of the protocols: the total speed limit of an instance on the server card."""

from typing import Any

from django import template

from apps.protocols.models import ProtocolInstance
from apps.protocols.services.speed_limit import ServerSpeedLimit

register = template.Library()


@register.inclusion_tag("protocols/_speed_limit.html")
def speed_limit_line(instance: ProtocolInstance) -> dict[str, Any]:
    """The limits of the instance, and a warning when upload is policed for want of ifb."""
    speed = ServerSpeedLimit(instance)
    applies = speed.applies
    return {
        "instance": instance,
        "limited": applies and bool(instance.download_limit or instance.upload_limit),
        "policed": applies and speed.upload_policed,
    }
