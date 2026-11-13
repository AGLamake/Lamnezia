from collections.abc import Sequence
from typing import Any

from django import template

from apps.servers.models import ServerStatus
from apps.servers.services.monitor import DayHealth
from apps.ssh.errors import ErrorCode, SshError

register = template.Library()

_BADGES = {ServerStatus.READY: "badge-success", ServerStatus.FAILED: "badge-danger"}
# Load in percent from which a meter turns amber and then red.
_BUSY = 75
_FULL = 90


@register.filter
def server_badge(status: str) -> str:
    """CSS class of the server status badge."""
    return _BADGES.get(ServerStatus(status), "badge-muted")


@register.filter
def load_tone(percent: float | None) -> str:
    """CSS class of a load meter: amber from 75 %, red from 90 %."""
    if percent is None or percent < _BUSY:
        return ""
    return "progress-bar-danger" if percent >= _FULL else "progress-bar-warning"


@register.filter
def ssh_error(code: str) -> str:
    """Human message of an SSH error code saved by the monitor."""
    return SshError(ErrorCode(code)).message


@register.inclusion_tag("servers/_trends.html")
def health_trends(history: Sequence[DayHealth]) -> dict[str, Any]:
    """Load by day as sparkline tiles, oldest day first; `history` is newest first."""
    days = list(reversed(history))
    return {
        "days": history,
        "latest": history[0],
        "since": days[0].day,
        "availability": [day.availability for day in days],
        "cpu": [day.cpu for day in days],
        "memory": [day.memory for day in days],
        "disk_free": [day.disk_free for day in days],
    }
