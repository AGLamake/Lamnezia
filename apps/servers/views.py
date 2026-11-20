from typing import Any

from django.conf import settings
from django.contrib import messages
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.accounts.permissions import SectionRequiredMixin
from apps.accounts.services.access import Section
from apps.operations.views import progress_context
from apps.protocols.services.inventory import server_containers
from apps.protocols.services.ipsec_reissue import IpsecIdentity
from apps.protocols.services.readdress import ServerReaddress
from apps.servers.forms import (
    CONNECTION_FIELDS,
    ProbeForm,
    ServerAddressForm,
    ServerFilterForm,
    ServerForm,
)
from apps.servers.models import CheckWarning, Server, ServerAddress
from apps.servers.services import probe
from apps.servers.services.addresses import ServerAddresses
from apps.servers.services.check import start_check
from apps.servers.services.history import latest_operation
from apps.servers.services.host_key import (
    forget_host_key,
    forget_old_address,
    trusted_host_key,
)
from apps.servers.services.monitor import daily_history


class ServerListView(SectionRequiredMixin, ListView):  # type: ignore[type-arg]
    section = Section.SERVERS
    template_name = "servers/list.html"
    context_object_name = "servers"

    def get_queryset(self) -> Any:
        self.filter_form = ServerFilterForm(self.request.GET or None)
        return self.filter_form.filter(Server.objects.select_related("latest_sample"))

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(
            filter_form=self.filter_form, has_servers=Server.objects.exists(), **kwargs
        )


class ServerDetailView(SectionRequiredMixin, DetailView):  # type: ignore[type-arg]
    """Server card: its containers and the progress or result of its latest operation."""

    section = Section.SERVERS

    model = Server
    template_name = "servers/detail.html"
    context_object_name = "server"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        server: Server = self.object
        latest = latest_operation(server)
        return super().get_context_data(
            warnings=[CheckWarning(code).label for code in server.warnings],
            containers=server_containers(server),
            history=daily_history(server, settings.MONITOR_HISTORY_DAYS),
            probe_form=ProbeForm(server=server),
            addresses=list(server.addresses.all()),
            address_form=ServerAddressForm(server=server),
            host_key=trusted_host_key(server),
            ipsec=IpsecIdentity.of(server),
            **(progress_context(latest) if latest else {}),
            **kwargs,
        )


class ServerCreateView(SectionRequiredMixin, CreateView):  # type: ignore[type-arg]
    section = Section.SERVERS
    model = Server
    form_class = ServerForm
    template_name = "servers/form.html"

    def form_valid(self, form: "ModelForm[Server]") -> HttpResponse:
        server = form.save()
        start_check(server, self.member)
        return HttpResponseRedirect(reverse("servers:detail", args=[server.pk]))


class ServerUpdateView(SectionRequiredMixin, UpdateView):  # type: ignore[type-arg]
    section = Section.SERVERS
    model = Server
    form_class = ServerForm
    template_name = "servers/form.html"
    context_object_name = "server"

    def form_valid(self, form: "ModelForm[Server]") -> HttpResponse:
        """A new address forgets the old SSH fingerprint; the keys and IPsec may need updating."""
        server = form.save()
        old_host, old_port = form.initial["host"], form.initial["port"]
        ip = self.client_ip
        forget_old_address(server, old_host, old_port, self.member, ip=ip)
        if CONNECTION_FIELDS & set(form.changed_data):
            start_check(server, self.member)
        if "host" in form.changed_data and _readdress_pending(server):
            return redirect("protocols:readdress", server_pk=server.pk)
        return HttpResponseRedirect(reverse("servers:detail", args=[server.pk]))


def _readdress_pending(server: Server) -> bool:
    return ServerReaddress(server).pending or IpsecIdentity.of(server) is not None


class ServerCheckView(SectionRequiredMixin, View):
    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = get_object_or_404(Server, pk=pk)
        start_check(server, self.member)
        return redirect("servers:detail", pk=server.pk)


class ServerHostKeyForgetView(SectionRequiredMixin, View):
    """Forget the SSH fingerprint of a reinstalled server; the check trusts its new key."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = get_object_or_404(Server, pk=pk)
        if forget_host_key(server, self.member, ip=self.client_ip):
            messages.success(
                request, _("The SSH fingerprint is forgotten: the check saves the new one.")
            )
            start_check(server, self.member)
        return redirect("servers:detail", pk=server.pk)


class ProbeInstallView(SectionRequiredMixin, View):
    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = get_object_or_404(Server, pk=pk)
        form = ProbeForm(request.POST, server=server)
        if form.is_valid():
            probe.start_install(server, form.cleaned_data["port"], self.member)
        else:
            for error in form.errors["port"]:
                messages.error(request, str(error))
        return redirect("servers:detail", pk=server.pk)


class ProbeRemoveView(SectionRequiredMixin, View):
    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        server = get_object_or_404(Server, pk=pk)
        probe.start_remove(server, self.member)
        return redirect("servers:detail", pk=server.pk)


class _AddressView(SectionRequiredMixin, View):
    """Changes of the other addresses of a server; the server page shows the result."""

    section = Section.SERVERS

    def addresses(self, pk: int) -> ServerAddresses:
        return ServerAddresses(get_object_or_404(Server, pk=pk), self.member, self.client_ip)

    def address(self, server: Server, address_pk: int) -> ServerAddress:
        return get_object_or_404(server.addresses, pk=address_pk)


class ServerAddressAddView(_AddressView):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        addresses = self.addresses(pk)
        form = ServerAddressForm(request.POST, server=addresses.server)
        if not form.is_valid():
            for error in form.errors["host"]:
                messages.error(request, str(error))
            return redirect("servers:detail", pk=pk)
        return redirect("operations:detail", pk=addresses.add(form.cleaned_data["host"]).pk)


class ServerAddressCheckView(_AddressView):
    def post(self, request: HttpRequest, pk: int, address_pk: int) -> HttpResponse:
        addresses = self.addresses(pk)
        operation = addresses.check(self.address(addresses.server, address_pk))
        return redirect("operations:detail", pk=operation.pk)


class ServerAddressRemoveView(_AddressView):
    """Keys sent to the removed address are moved to the server's own one on the next page."""

    def post(self, request: HttpRequest, pk: int, address_pk: int) -> HttpResponse:
        addresses = self.addresses(pk)
        addresses.remove(self.address(addresses.server, address_pk))
        if ServerReaddress(addresses.server).outdated:
            return redirect("protocols:readdress", server_pk=pk)
        return redirect("servers:detail", pk=pk)
