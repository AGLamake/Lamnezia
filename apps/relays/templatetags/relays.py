"""Template tags of the relay cards: a moment the agent wrote, the channel load and its chart,
the traffic of split groups abroad and straight out with its chart, the settings form."""

from datetime import UTC, datetime, timedelta
from typing import Any

from django import template
from django.template.defaultfilters import filesizeformat
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from apps.groups.models import ServerGroup
from apps.relays.forms import RelaySettingsForm
from apps.relays.models import Relay
from apps.relays.services.health import GroupHealth, SplitTraffic
from apps.relays.services.load import (
    DAY,
    WEEK,
    Averages,
    LoadHistory,
    LoadSeries,
    SplitHistory,
    SplitSeries,
)

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


@register.inclusion_tag("relays/_split_traffic.html")
def relay_split_traffic(relay: Relay) -> dict[str, Any]:
    """The traffic of the split groups of a relay abroad and straight out: now and its chart."""
    return _split_traffic(
        SplitHistory.of_relay(relay, timezone.now()), relay.split_traffic, f"relay-{relay.pk}"
    )


@register.inclusion_tag("relays/_split_traffic.html")
def group_split_traffic(group: ServerGroup, health: GroupHealth | None) -> dict[str, Any]:
    """The traffic of a split group abroad and straight out: now and its chart."""
    history = SplitHistory(group.split_samples.all(), timezone.now())
    return _split_traffic(history, health.traffic if health else None, f"group-{group.pk}")


def _split_traffic(history: SplitHistory, now: SplitTraffic | None, key: str) -> dict[str, Any]:
    day = history.series(DAY, 24)
    week = history.series(WEEK, 28)
    return {
        "now": now,
        "key": key,
        "week": week,
        "day_chart": split_chart(day, DAY, "H:i"),
        "week_chart": split_chart(week, WEEK, "D H:i"),
    }


def load_chart(series: LoadSeries, period: timedelta, label: str) -> dict[str, Any]:
    """Arguments of the `bar_chart` tag for the channel load: both directions."""
    return _chart(
        series.since,
        period,
        label,
        {_("From clients"): series.from_clients, _("To clients"): series.to_clients},
    )


def split_chart(series: SplitSeries, period: timedelta, label: str) -> dict[str, Any]:
    """Arguments of the `bar_chart` tag for split groups: abroad and straight out."""
    return _chart(
        series.since,
        period,
        label,
        {_("Through the channel"): series.abroad, _("Straight out"): series.direct},
    )


def _chart(
    since: datetime, period: timedelta, label: str, series: dict[str, Averages]
) -> dict[str, Any]:
    """A bar per bucket, labelled by its local start; a bucket without readings is a gap, not
    zero."""
    buckets = len(next(iter(series.values())))
    width = period / buckets
    return {
        "labels": [
            date_format(timezone.localtime(since + width * index), label)
            for index in range(buckets)
        ],
        "series": [
            [None if rate is None else round(rate) for rate in rates] for rates in series.values()
        ],
        "names": list(series),
    }
