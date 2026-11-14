"""Template filters of the relay cards: a moment the agent wrote and the load of the channel."""

from datetime import UTC, datetime

from django import template
from django.template.defaultfilters import filesizeformat
from django.utils.translation import gettext as _

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
