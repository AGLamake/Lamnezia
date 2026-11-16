"""Buttons of the relay card on the server page: install, update, remove and the settings."""

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from apps.accounts.permissions import AdminRequiredMixin
from apps.relays.errors import RelayError
from apps.relays.forms import RelaySettingsForm
from apps.relays.models import Relay
from apps.relays.services.deployment import INSTALL_KIND, REMOVE_KIND, UPDATE_KIND, start
from apps.servers.models import Server


class RelayActionView(AdminRequiredMixin, View):
    kind = ""

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = get_object_or_404(Server, pk=pk)
        try:
            operation = start(self.kind, server, user=self.member, ip=self.client_ip)
        except RelayError as error:
            messages.error(request, f"{error.message} {error.hint}")
            return redirect("servers:detail", pk=server.pk)
        return redirect("operations:detail", pk=operation.pk)


class RelaySettingsView(AdminRequiredMixin, View):
    """Save the agent settings and write them to the relay by a background update."""

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
