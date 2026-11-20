from typing import Any

from django.conf import settings
from django.contrib import messages
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseBase,
)
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.permissions import AdminRequiredMixin, SectionRequiredMixin, StaffRequiredMixin
from apps.accounts.services.access import Level, Section
from apps.accounts.services.zone import Zone
from apps.dashboard.forms import ShowcaseForm
from apps.dashboard.services.attention import Attention
from apps.dashboard.services.overview import Overview
from apps.dashboard.services.palette import MIN_LENGTH, QuickSearch
from apps.dashboard.services.snooze import Snooze
from apps.dashboard.tasks import send_reports_now

_LAYOUTS = {"admin": "layouts/admin.html", "portal": "layouts/portal.html"}
# Made-up numbers for the sparklines of the showcase; None is a day without data.
_DEMO_SERIES = {
    "demo_availability": [100, 99.3, 100, 97.9, 100, 100, 99.6, None, 100, 98.8, 100, 99.6],
    "demo_cpu": [18, 22, 25, 19, 31, 44, 38, 27, 21, 24, 19, 23],
    "demo_traffic": [31, 35, 29, 40, 52, 61, 47, 39, 44, 41, 49, 48],
}


class DashboardView(SectionRequiredMixin, TemplateView):
    """The overview: key numbers, what needs attention, the week's traffic and the servers."""

    section = Section.OVERVIEW

    template_name = "dashboard/index.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        attention = Attention(self.member)
        self.request.attention = attention  # type: ignore[attr-defined]  # the menu reuses it
        overview = Overview(self.member, attention.servers)
        week = overview.week
        notices, hidden = _snooze(self.request).split(attention.notices())
        return super().get_context_data(
            servers=attention.servers,
            notices=notices,
            hidden_notices=hidden,
            overview=overview,
            whole_zone=Zone(self.member).is_whole,
            chart={
                "labels": [date_format(point.at, "D") for point in week],
                "series": [[point.tx for point in week], [point.rx for point in week]],
                "names": [_("Downloaded"), _("Uploaded")],
            },
            **kwargs,
        )


class NoticeHideView(SectionRequiredMixin, View):
    """Hides one notice of the overview until tomorrow."""

    section = Section.OVERVIEW
    write_level = Level.VIEW  # the member's own view of the page, nothing shared changes

    def post(self, request: HttpRequest) -> HttpResponse:
        key = request.POST.get("key", "")
        if not Snooze.valid(key):
            return HttpResponseBadRequest()
        _snooze(request).hide(key)
        return redirect("dashboard:index")


class NoticeShowView(SectionRequiredMixin, View):
    """Shows the notices hidden today again."""

    section = Section.OVERVIEW
    write_level = Level.VIEW  # the member's own view of the page, nothing shared changes

    def post(self, request: HttpRequest) -> HttpResponse:
        _snooze(request).show_all()
        return redirect("dashboard:index")


class ReportSendView(AdminRequiredMixin, View):
    """Sends the report to the staff now, to see how it reads; the schedule stays as it is."""

    def post(self, request: HttpRequest) -> HttpResponse:
        send_reports_now.delay()
        messages.success(request, _("The report is on its way. The result is in the log."))
        return redirect("notifications:log")


def _snooze(request: HttpRequest) -> Snooze:
    return Snooze(request.session, timezone.localdate())


class QuickSearchView(StaffRequiredMixin, TemplateView):
    """Results of the command palette, loaded over HTMX as the staff member types."""

    template_name = "dashboard/_search_results.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        text = self.request.GET.get("q", "").strip()
        return super().get_context_data(
            text=text,
            short=len(text) < MIN_LENGTH,
            groups=QuickSearch(self.member).groups(text),
            **kwargs,
        )


class DevPageView(TemplateView):
    """Developer page, hidden outside DEBUG."""

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not settings.DEBUG:
            raise Http404
        return super().dispatch(request, *args, **kwargs)


class ComponentShowcaseView(DevPageView):
    """All UI components on one page; `?layout=portal` shows them in the client layout."""

    template_name = "dashboard/dev_ui.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        layout = self.request.GET.get("layout", "admin")
        form = ShowcaseForm(self.request.GET if "name" in self.request.GET else None)
        if form.is_bound:
            form.is_valid()
        return super().get_context_data(
            layout=_LAYOUTS.get(layout, _LAYOUTS["admin"]), form=form, **_DEMO_SERIES, **kwargs
        )


class ShowcaseModalView(DevPageView):
    template_name = "dashboard/dev_ui_modal.html"
