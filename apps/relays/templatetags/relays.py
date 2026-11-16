"""Template tags of the relay cards: a moment the agent wrote, the channel load and its chart."""

from datetime import UTC, datetime
from typing import Any

from django import template
from django.template.defaultfilters import filesizeformat
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.relays.models import Relay
from apps.relays.services.load import DAY, WEEK, LoadHistory

register = template.Library()


@register.filter
def unix_time(seconds: int | None) -> datetime | None:
    """A moment in Unix seconds, as the agent writes it, for the `date` filter."""
    return datetime.fromtimestamp(seconds, UTC) if seconds is not None else None


@register.filter
def per_second(rate: float | None) -> str:
    """Bytes per second as "1.2 MB/s"; a dash when unknown."""
    if rate is None:
        return "—"
    return _("%(size)s/s") % {"size": filesizeformat(rate)}


@register.inclusion_tag("relays/_load.html")
def relay_load(relay: Relay) -> dict[str, Any]:
    """The channel load of the last day by hour and of the last week by six hours."""
    history = LoadHistory(relay, timezone.now())
    week = history.series(WEEK, 28)
    return {"day": history.series(DAY, 24), "week": week}
