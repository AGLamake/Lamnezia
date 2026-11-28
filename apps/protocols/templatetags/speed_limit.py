"""Template tags of the protocols: the speed limits of an instance on the server card."""

from typing import Any

from django import template

from apps.protocols.models import ProtocolInstance
from apps.protocols.services.speed_limit import ServerSpeedLimit, ShapingState

register = template.Library()


@register.inclusion_tag("protocols/_speed_limit.html")
def speed_limit_line(instance: ProtocolInstance) -> dict[str, Any]:
    """The limits of the instance, a warning when upload is policed for want of ifb and one
    when the latest reading found the applied limits gone from the container."""
    speed = ServerSpeedLimit(instance)
    applies = speed.applies
    return {
        "instance": instance,
        "limited": applies and bool(instance.download_limit or instance.upload_limit),
        "policed": applies and speed.upload_policed,
        "shaping_lost": ShapingState(instance).lost(),
    }
