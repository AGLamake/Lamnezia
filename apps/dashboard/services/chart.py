"""Traffic statistics on a page, shared by the staff statistics and the user's own usage in the
portal: the period switches and the arguments of the `bar_chart` tag.
"""

from dataclasses import dataclass
from typing import Any

from django.http import QueryDict
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from apps.dashboard.services.stats import Period, Point, Window


@dataclass(frozen=True)
class PeriodLink:
    """A period switch of the page: the same filters with another period."""

    title: str
    query: str
    current: bool


def period_links(query: QueryDict, selected: Period) -> list[PeriodLink]:
    links = []
    for period in Period:
        switched = query.copy()
        switched["period"] = period.value
        links.append(PeriodLink(str(period.label), switched.urlencode(), period == selected))
    return links


def traffic_chart(series: list[Point], window: Window) -> dict[str, Any]:
    """Arguments of the `bar_chart` tag; rx is what clients uploaded."""
    label = "H:i" if window.hourly else "MONTH_DAY_FORMAT"
    return {
        "labels": [date_format(point.at, label) for point in series],
        "series": [[point.tx for point in series], [point.rx for point in series]],
        "names": [_("Downloaded"), _("Uploaded")],
    }
