"""Traffic statistics for the staff: the page with the chart and tables, and its CSV.

A moderator sees their zone and not the storage report.
"""

import csv
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.models import User
from apps.accounts.permissions import StaffRequiredMixin
from apps.dashboard.forms import StatsFilterForm
from apps.dashboard.services.chart import period_links, traffic_chart
from apps.dashboard.services.forecast import LimitForecast
from apps.dashboard.services.stats import (
    KeyTraffic,
    TrafficStats,
    Window,
    inactive,
    top_users,
)
from apps.dashboard.services.storage import StorageReport
from apps.keys.services.instances import instances
from apps.traffic.services.online import OnlineRule

# A spreadsheet runs a cell starting with these as a formula; device names are typed by users.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _stats(request: HttpRequest, member: User) -> tuple[StatsFilterForm, Window, TrafficStats]:
    form = StatsFilterForm(request.GET, member=member)
    window = Window(form.selected_period, timezone.now())
    return form, window, TrafficStats(form.scope, window, OnlineRule.from_settings())


class StatsView(StaffRequiredMixin, TemplateView):
    template_name = "dashboard/stats.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        form, window, stats = _stats(self.request, self.member)
        series = stats.series()
        keys = stats.by_key()
        return super().get_context_data(
            form=form,
            scope=form.scope,
            received=sum(point.rx for point in series),
            sent=sum(point.tx for point in series),
            chart=traffic_chart(series, window),
            top=top_users(keys),
            online=stats.online(),
            inactive=inactive(keys),
            unavailable=stats.unavailable_sources(),
            forecast=LimitForecast(window.now).soonest(form.scope.users()),
            storage=StorageReport().rows() if self.member.is_admin else [],
            query=self.request.GET.urlencode(),
            periods=period_links(self.request.GET, form.selected_period),
            **kwargs,
        )


class StatsExportView(StaffRequiredMixin, View):
    """Traffic of every key of the scope over the period, one row per key."""

    def get(self, request: HttpRequest) -> HttpResponse:
        form, window, stats = _stats(request, self.member)
        name = f"traffic-{form.selected_period}-{window.now:%Y-%m-%d}.csv"
        response = HttpResponse(
            content_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
        # The byte order mark makes Excel read the file as UTF-8, not in the local code page.
        response.write("﻿")
        writer = csv.writer(response)
        writer.writerow(_csv_header())
        titles = instances.titles()
        writer.writerows(_csv_row(row, titles) for row in stats.by_key())
        return response


def _csv_header() -> list[str]:
    return [
        _("Server"),
        _("Device"),
        _("User"),
        _("Status"),
        _("Upload, bytes"),
        _("Download, bytes"),
    ]


def _csv_row(row: KeyTraffic, titles: dict[str, str]) -> list[str | int]:
    key = row.key
    return [
        _cell(titles.get(key.source, key.source)),
        _cell(key.name),
        _cell(key.user.username if key.user else ""),
        key.get_status_display(),
        *((row.rx, row.tx) if row.has_data else ("", "")),
    ]


def _cell(text: str) -> str:
    return f"'{text}" if text.startswith(_FORMULA_START) else text
