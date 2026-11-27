"""Buttons of the relay card on the server page: install, update, remove and the settings; and
the network list of a split group on the group page: its mode, sources and own entries."""

from typing import Any

from django.contrib import messages
from django.db import transaction
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import ngettext
from django.views import View

from apps.accounts.permissions import SectionRequiredMixin
from apps.accounts.services.access import Section
from apps.audit.services import journal as audit
from apps.groups.models import GroupListMode
from apps.relays.errors import RelayError
from apps.relays.forms import (
    RelaySettingsForm,
    SplitEntryForm,
    SplitListModeForm,
    SplitSourceForm,
)
from apps.relays.models import Relay, SplitEntry, SplitSource
from apps.relays.services.deployment import INSTALL_KIND, REMOVE_KIND, UPDATE_KIND, start
from apps.relays.services.split_lists import change_list_mode, update_soon

# The block of the network list on the group page.
_LISTS = "#split-lists"


class RelayActionView(SectionRequiredMixin, View):
    section = Section.SERVERS
    whole_zone_only = True
    kind = ""

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = get_object_or_404(self.zone.servers(), pk=pk)
        try:
            operation = start(self.kind, server, user=self.member, ip=self.client_ip)
        except RelayError as error:
            messages.error(request, f"{error.message} {error.hint}")
            return redirect("servers:detail", pk=server.pk)
        return redirect("operations:detail", pk=operation.pk)


class RelaySettingsView(SectionRequiredMixin, View):
    """Save the agent settings and write them to the relay by a background update."""

    section = Section.SERVERS
    whole_zone_only = True

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        relay = get_object_or_404(Relay, server_id=pk, installed_at__isnull=False)
        form = RelaySettingsForm(request.POST, instance=relay)
        if not form.is_valid():
            for errors in form.errors.values():
                messages.error(request, " ".join(map(str, errors)))
            return redirect("servers:detail", pk=pk)
        if not form.has_changed():
            return redirect("servers:detail", pk=pk)
        form.save()
        operation = start(UPDATE_KIND, relay.server, user=self.member, ip=self.client_ip)
        return redirect("operations:detail", pk=operation.pk)


class RelayInstallView(RelayActionView):
    kind = INSTALL_KIND


class RelayUpdateView(RelayActionView):
    kind = UPDATE_KIND


class RelayRemoveView(RelayActionView):
    kind = REMOVE_KIND


def _to_lists(group_id: int) -> HttpResponse:
    response = redirect("groups:detail", pk=group_id)
    response["Location"] += _LISTS
    return response


def _report(request: HttpRequest, form: "ModelForm[Any]") -> None:
    for errors in form.errors.values():
        messages.error(request, " ".join(map(str, errors)))


class SplitSourceAddView(SectionRequiredMixin, View):
    """Add a list to the group and download it in the background at once."""

    section = Section.SERVERS
    whole_zone_only = True

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        group = get_object_or_404(self.zone.groups(), pk=pk)
        form = SplitSourceForm(request.POST, group=group)
        if not form.is_valid():
            _report(request, form)
            return _to_lists(group.pk)
        with transaction.atomic():
            source = form.save()
            audit.log_request(request, "relays.split_source_add", source, url=source.url)
            update_soon()
        return _to_lists(group.pk)


class SplitSourceToggleView(SectionRequiredMixin, View):
    """Turn a list off, or on again; a list turned on is downloaded at once."""

    section = Section.SERVERS
    whole_zone_only = True

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        source = get_object_or_404(SplitSource, pk=pk)
        with transaction.atomic():
            source.enabled = not source.enabled
            source.save(update_fields=["enabled"])
            audit.log_request(request, "relays.split_source_toggle", source, enabled=source.enabled)
            if source.enabled:
                update_soon()
        return _to_lists(source.group_id)


class SplitSourceDeleteView(SectionRequiredMixin, View):
    section = Section.SERVERS
    whole_zone_only = True

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        source = get_object_or_404(SplitSource, pk=pk)
        with transaction.atomic():
            audit.log_request(request, "relays.split_source_delete", source, url=source.url)
            source.delete()
        return _to_lists(source.group_id)


class SplitEntryAddView(SectionRequiredMixin, View):
    section = Section.SERVERS
    whole_zone_only = True

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        group = get_object_or_404(self.zone.groups(), pk=pk)
        form = SplitEntryForm(request.POST, group=group)
        if not form.is_valid():
            _report(request, form)
            return _to_lists(group.pk)
        with transaction.atomic():
            entry = form.save()
            audit.log_request(
                request,
                "relays.split_entry_add",
                entry,
                value=entry.value,
                direction=entry.direction,
            )
        return _to_lists(group.pk)


class SplitEntryDeleteView(SectionRequiredMixin, View):
    section = Section.SERVERS
    whole_zone_only = True

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        entry = get_object_or_404(SplitEntry, pk=pk)
        with transaction.atomic():
            audit.log_request(request, "relays.split_entry_delete", entry, value=entry.value)
            entry.delete()
        return _to_lists(entry.group_id)


class SplitListModeView(SectionRequiredMixin, View):
    """Move the list of a group between the blacklist and the whitelist; its sources are turned
    off, since a list chosen for one mode sends the wrong traffic in the other."""

    section = Section.SERVERS
    whole_zone_only = True

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        group = get_object_or_404(self.zone.groups(), pk=pk)
        form = SplitListModeForm(request.POST)
        if not form.is_valid():
            _report(request, form)
            return _to_lists(group.pk)
        mode = GroupListMode(form.cleaned_data["list_mode"])
        turned_off = change_list_mode(group, mode, user=self.member, ip=self.client_ip)
        if turned_off:
            text = ngettext(
                "The list mode is changed and %(count)d list is turned off: turn on only a list "
                "that suits the new mode.",
                "The list mode is changed and %(count)d lists are turned off: turn on only lists "
                "that suit the new mode.",
                turned_off,
            )
            messages.warning(request, text % {"count": turned_off})
        return _to_lists(group.pk)
