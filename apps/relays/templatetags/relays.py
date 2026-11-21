"""Template tags of the relay cards: a moment the agent wrote, the channel load and its chart,
the settings form."""

from datetime import UTC, datetime, timedelta
from typing import Any

from django import template
from django.template.defaultfilters import filesizeformat
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from apps.relays.forms import RelaySettingsForm
from apps.relays.models import Relay
from apps.relays.services.load import DAY, WEEK, LoadHistory, LoadSeries

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


@register.simple_tag
def relay_settings_form(relay: Relay) -> RelaySettingsForm:
    """The settings form of the card, filled with what the relay has."""
    return RelaySettingsForm(instance=relay)


@register.inclusion_tag("relays/_load.html")
def relay_load(relay: Relay) -> dict[str, Any]:
    """The channel load of the last day by hour and of the last week by six hours."""
    history = LoadHistory(relay, timezone.now())
    day = history.series(DAY, 24)
    week = history.series(WEEK, 28)
    return {
        "relay": relay,
        "day": day,
        "week": week,
        "day_chart": load_chart(day, DAY, "H:i"),
        "week_chart": load_chart(week, WEEK, "D H:i"),
    }


def load_chart(series: LoadSeries, period: timedelta, label: str) -> dict[str, Any]:
    """Arguments of the `bar_chart` tag: a bar per bucket, labelled by its local start; a bucket
    without readings is a gap, not zero."""
    width = period / len(series.from_clients)
    return {
        "labels": [
            date_format(timezone.localtime(series.since + width * index), label)
            for index in range(len(series.from_clients))
        ],
        "series": [
            [None if rate is None else round(rate) for rate in series.from_clients],
            [None if rate is None else round(rate) for rate in series.to_clients],
        ],
        "names": [_("From clients"), _("To clients")],
    }
