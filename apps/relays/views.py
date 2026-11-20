"""Buttons of the relay card on the server page: install, update, remove and the settings; and
the network list of a split group on the group page: its sources and own entries."""

from django.contrib import messages
from django.db import transaction
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from apps.accounts.permissions import SectionRequiredMixin
from apps.accounts.services.access import Section
from apps.audit.services import journal as audit
from apps.groups.models import ServerGroup
from apps.relays.errors import RelayError
from apps.relays.forms import RelaySettingsForm, SplitEntryForm, SplitSourceForm
from apps.relays.models import Relay, SplitEntry, SplitSource
from apps.relays.services.deployment import INSTALL_KIND, REMOVE_KIND, UPDATE_KIND, start
from apps.relays.services.split_lists import update_soon
from apps.servers.models import Server

# The block of the network list on the group page.
_LISTS = "#split-lists"


class RelayActionView(SectionRequiredMixin, View):
    section = Section.SERVERS
    kind = ""

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = get_object_or_404(Server, pk=pk)
        try:
            operation = start(self.kind, server, user=self.member, ip=self.client_ip)
        except RelayError as error:
            messages.error(request, f"{error.message} {error.hint}")
            return redirect("servers:detail", pk=server.pk)
        return redirect("operations:detail", pk=operation.pk)


class RelaySettingsView(SectionRequiredMixin, View):
    """Save the agent settings and write them to the relay by a background update."""

    section = Section.SERVERS

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


def _report(request: HttpRequest, form: "ModelForm[SplitSource] | ModelForm[SplitEntry]") -> None:
    for errors in form.errors.values():
        messages.error(request, " ".join(map(str, errors)))


class SplitSourceAddView(SectionRequiredMixin, View):
    """Add a list to the group and download it in the background at once."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        group = get_object_or_404(ServerGroup, pk=pk)
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

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        source = get_object_or_404(SplitSource, pk=pk)
        with transaction.atomic():
            audit.log_request(request, "relays.split_source_delete", source, url=source.url)
            source.delete()
        return _to_lists(source.group_id)


class SplitEntryAddView(SectionRequiredMixin, View):
    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        group = get_object_or_404(ServerGroup, pk=pk)
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

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        entry = get_object_or_404(SplitEntry, pk=pk)
        with transaction.atomic():
            audit.log_request(request, "relays.split_entry_delete", entry, value=entry.value)
            entry.delete()
        return _to_lists(entry.group_id)
