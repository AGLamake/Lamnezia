from typing import Any, cast

from django.contrib import messages
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.utils.functional import cached_property
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import FormView

from amnezia_core.containers import WG_CONTAINERS
from amnezia_core.openvpn import OPENVPN_CONTAINER
from amnezia_core.proxies import (
    MTPROXY_CONTAINER,
    SOCKS5_CONTAINER,
    TELEMT_CONTAINER,
    TPROXY_CONTAINER,
)
from amnezia_core.xray import XRAY_CONTAINER
from apps.accounts.models import User
from apps.accounts.permissions import SectionRequiredMixin
from apps.accounts.services.access import Section
from apps.accounts.services.zone import Zone
from apps.audit.services import journal as audit
from apps.groups.models import ServerGroup
from apps.keys.services.counts import InstanceKeyCount
from apps.keys.services.xray_profiles import prepare_all
from apps.protocols.forms import (
    AwgMigrationForm,
    AwgUpgradeForm,
    IpsecReissueForm,
    ProtocolSettingsForm,
    ReaddressForm,
    RemoveContainerForm,
    SpeedLimitForm,
    SpeedTestForm,
)
from apps.protocols.install_form import InstallForm
from apps.protocols.models import CONTAINER_TITLES, ProtocolInstance, ServiceInstance
from apps.protocols.service_settings_forms import (
    MtProtoSettingsForm,
    ServiceSettingsForm,
    Socks5SettingsForm,
    TProxySettingsForm,
)
from apps.protocols.services import metadata_block, offload, speed_test
from apps.protocols.services.awg_migration import (
    MigrationState,
    can_migrate,
    start_finish,
    start_migration,
)
from apps.protocols.services.awg_upgrade import can_upgrade, start_awg_upgrade
from apps.protocols.services.dns_rebuild import start_dns_rebuild
from apps.protocols.services.importer import start_import
from apps.protocols.services.installation import InstallChoice, install_choices, start_install
from apps.protocols.services.ipsec_reissue import IpsecIdentity, start_reissue
from apps.protocols.services.lifecycle import (
    LOGS_KIND,
    REMOVE_KIND,
    RESTART_KIND,
    RESTART_SERVICE_KIND,
    SERVICE_LOGS_KIND,
    STOP_KIND,
    STOP_SERVICE_KIND,
    start_container_action,
    start_service_action,
)
from apps.protocols.services.proxy_log import start_log_deletion
from apps.protocols.services.proxy_secret import renews_secret, start_secret_renewal
from apps.protocols.services.readdress import ServerReaddress
from apps.protocols.services.service_removal import start_service_removal
from apps.protocols.services.settings_info import SettingsInfo
from apps.protocols.services.speed_limit import ServerSpeedLimit
from apps.protocols.services.speed_test import RunningTest
from apps.protocols.settings_forms import OpenVpnSettingsForm, SettingsForm, XraySettingsForm
from apps.servers.models import Server
from config.security import policy_allowing

# The settings form of each container that has a settings page.
SETTINGS_FORMS: dict[str, type[SettingsForm]] = {
    **dict.fromkeys(WG_CONTAINERS, ProtocolSettingsForm),
    XRAY_CONTAINER: XraySettingsForm,
    OPENVPN_CONTAINER: OpenVpnSettingsForm,
}
SERVICE_SETTINGS_FORMS: dict[str, type[ServiceSettingsForm]] = {
    SOCKS5_CONTAINER: Socks5SettingsForm,
    MTPROXY_CONTAINER: MtProtoSettingsForm,
    TELEMT_CONTAINER: MtProtoSettingsForm,
    TPROXY_CONTAINER: TProxySettingsForm,
}

PASSWORD_VIEW = "protocols.service_password_view"
LINKS_VIEW = "protocols.service_links_view"

# Container actions started by a button on the server card.
_ACTION_KINDS = {"restart": RESTART_KIND, "stop": STOP_KIND, "logs": LOGS_KIND}
_SERVICE_ACTION_KINDS = {
    "restart": RESTART_SERVICE_KIND,
    "stop": STOP_SERVICE_KIND,
    "logs": SERVICE_LOGS_KIND,
}
_OFFLOAD_KINDS = {
    "check": offload.CHECK_KIND,
    "enable": offload.ENABLE_KIND,
    "disable": offload.DISABLE_KIND,
}
_METADATA_KINDS = {
    "check": metadata_block.CHECK_KIND,
    "enable": metadata_block.ENABLE_KIND,
    "disable": metadata_block.DISABLE_KIND,
}


def _services(zone: Zone) -> QuerySet[ServiceInstance]:
    """Services on the servers of the zone; any other service answers 404."""
    return zone.narrow(ServiceInstance.objects.select_related("server"), server="server")


class ServerImportView(SectionRequiredMixin, View):
    """Start reading the server's containers and clients; the server card shows the progress."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, server_pk: int) -> HttpResponse:
        server = get_object_or_404(self.zone.servers(), pk=server_pk)
        start_import(server, self.member)
        return redirect("servers:detail", pk=server.pk)


class ServerInstallView(SectionRequiredMixin, FormView):  # type: ignore[type-arg]
    """What can be installed on the server; the choice starts the install operation."""

    section = Section.SERVERS

    template_name = "protocols/install.html"
    form_class = InstallForm

    # Looked up after the role check of the view, so a guest cannot tell which servers exist.
    @cached_property
    def server(self) -> Server:
        return get_object_or_404(self.zone.servers(), pk=self.kwargs["server_pk"])

    @cached_property
    def choices(self) -> list[InstallChoice]:
        return install_choices(self.server)

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "server": self.server, "choices": self.choices}

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(server=self.server, choices=self.choices, **kwargs)

    def form_valid(self, form: InstallForm) -> HttpResponse:
        operation = start_install(self.server, form.request, self.member, ip=self.client_ip)
        return redirect("operations:detail", pk=operation.pk)


class OffloadView(SectionRequiredMixin, View):
    """Check TSO/GSO of the containers, or turn the workaround on or off; the card shows it."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, server_pk: int, action: str) -> HttpResponse:
        if action not in _OFFLOAD_KINDS:
            raise Http404
        server = get_object_or_404(self.zone.servers(), pk=server_pk)
        offload.start(_OFFLOAD_KINDS[action], server, self.member, ip=self.client_ip)
        return redirect("servers:detail", pk=server.pk)


class MetadataBlockView(SectionRequiredMixin, View):
    """Check the cloud metadata block, or close or open the service; the card shows it."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, server_pk: int, action: str) -> HttpResponse:
        if action not in _METADATA_KINDS:
            raise Http404
        server = get_object_or_404(self.zone.servers(), pk=server_pk)
        metadata_block.start(_METADATA_KINDS[action], server, self.member, ip=self.client_ip)
        return redirect("servers:detail", pk=server.pk)


class SpeedTestView(SectionRequiredMixin, FormView):  # type: ignore[type-arg]
    """The speed test by port: the running test (measured here or with iperf3), or a new one.

    A browser test lets this page's scripts reach exactly its ports (CSP `connect-src`); the
    page holds the session's secret path, so it is never cached.
    """

    section = Section.SERVERS

    template_name = "protocols/speed_test.html"
    form_class = SpeedTestForm

    @cached_property
    def server(self) -> Server:
        return get_object_or_404(self.zone.servers(), pk=self.kwargs["server_pk"])

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "server": self.server}

    @cached_property
    def running(self) -> RunningTest | None:
        return RunningTest.of(self.server, timezone.now())

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(server=self.server, running=self.running, **kwargs)

    def render_to_response(self, context: dict[str, Any], **kwargs: Any) -> HttpResponse:
        response = super().render_to_response(context, **kwargs)
        if self.running and (targets := self.running.targets):
            origins = [target.origin for target in targets]
            response.headers["Content-Security-Policy"] = policy_allowing(origins)
            add_never_cache_headers(response)
        return response

    def form_valid(self, form: SpeedTestForm) -> HttpResponse:
        operation = speed_test.start(self.server, form.plan, self.member, ip=self.client_ip)
        return redirect("operations:detail", pk=operation.pk)


class SpeedTestStopView(SectionRequiredMixin, View):
    """Remove the iperf3 container before its time is up."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, server_pk: int) -> HttpResponse:
        server = get_object_or_404(self.zone.servers(), pk=server_pk)
        operation = speed_test.stop(server, self.member, ip=self.client_ip)
        return redirect("operations:detail", pk=operation.pk)


class InstanceMixin:
    """The instance from the `pk` of the URL, with its server, if it is one of `containers`.

    An instance whose container is removed has nothing to set up or run: only its removal page
    sets `allow_removed`.
    """

    allow_removed = False
    containers: tuple[str, ...] = tuple(CONTAINER_TITLES)

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        # Runs after the role check of the view, so a guest cannot tell which instances exist.
        member = cast(User, request.user)
        instances = Zone(member).narrow(
            ProtocolInstance.objects.select_related("server"), server="server"
        )
        if not self.allow_removed:
            instances = instances.filter(container_removed_at__isnull=True)
        self.instance = get_object_or_404(instances, pk=kwargs["pk"], container__in=self.containers)
        return super().dispatch(request, *args, **kwargs)  # type: ignore[misc, no-any-return]


class ProtocolSettingsView(SectionRequiredMixin, InstanceMixin, FormView):  # type: ignore[type-arg]
    """The settings of a protocol on its own form; saving starts the operation that applies them."""

    section = Section.SERVERS

    template_name = "protocols/settings.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if self.instance.container not in SETTINGS_FORMS:
            return _settings_info(request, self.instance)
        return super().get(request, *args, **kwargs)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if self.instance.container not in SETTINGS_FORMS:
            raise Http404
        if self.group is not None:
            messages.error(request, _("The settings of a group member change on the group page."))
            return redirect("groups:settings", pk=self.group.pk)
        return super().post(request, *args, **kwargs)

    @cached_property
    def group(self) -> ServerGroup | None:
        """The group of a member: its servers are clones, so only the group changes settings."""
        member = getattr(self.instance, "group_member", None)
        return None if member is None else member.group

    def get_form_class(self) -> type[SettingsForm]:
        return SETTINGS_FORMS[self.instance.container]

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "instance": self.instance}

    def get_form(self, form_class: type[SettingsForm] | None = None) -> SettingsForm:
        form: SettingsForm = super().get_form(form_class)
        if self.group is not None:
            for field in form.fields.values():
                field.disabled = True
        return form

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        keys = InstanceKeyCount.of(self.instance.source)
        speed = ServerSpeedLimit(self.instance)
        return super().get_context_data(
            instance=self.instance,
            keys=keys.total,
            imported=keys.imported,
            group=self.group,
            speed_form=SpeedLimitForm(instance=self.instance) if speed.applies else None,
            upload_policed=speed.applies and speed.upload_policed,
            **kwargs,
        )

    def form_valid(self, form: SettingsForm) -> HttpResponse:
        operation = form.start(self.member)
        return redirect("operations:detail", pk=operation.pk)


class SpeedLimitView(SectionRequiredMixin, InstanceMixin, View):
    """Saves the total speed limit of the protocol; the traffic app applies it on the server."""

    section = Section.SERVERS

    containers = tuple(WG_CONTAINERS)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        speed = ServerSpeedLimit(self.instance)
        if not speed.applies:
            raise Http404
        form = SpeedLimitForm(request.POST, instance=self.instance)
        if not form.is_valid():
            for errors in form.errors.as_data().values():
                for error in errors:
                    messages.error(request, " ".join(error.messages))
        else:
            data = form.cleaned_data
            changed = speed.save(
                data["download_limit"], data["upload_limit"], self.member, self.client_ip
            )
            if changed:
                messages.success(request, _("The speed limit is saved and is being applied."))
        return redirect("protocols:settings", pk=pk)


class ContainerActionView(SectionRequiredMixin, InstanceMixin, View):
    """Restart or stop the container, or read its log; the operation page shows the result."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int, action: str) -> HttpResponse:
        if action not in _ACTION_KINDS:
            raise Http404
        operation = start_container_action(_ACTION_KINDS[action], self.instance, self.member)
        return redirect("operations:detail", pk=operation.pk)


class ContainerRemoveView(SectionRequiredMixin, InstanceMixin, FormView):  # type: ignore[type-arg]
    """Removal of the container, confirmed by typing the server name."""

    section = Section.SERVERS

    template_name = "protocols/remove.html"
    form_class = RemoveContainerForm
    allow_removed = True

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "instance": self.instance}

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        keys = InstanceKeyCount.of(self.instance.source).total
        return super().get_context_data(instance=self.instance, keys=keys, **kwargs)

    def form_valid(self, form: RemoveContainerForm) -> HttpResponse:
        operation = start_container_action(
            REMOVE_KIND,
            self.instance,
            self.member,
            delete_backups=form.cleaned_data["delete_backups"],
        )
        return redirect("operations:detail", pk=operation.pk)


class ServiceSettingsView(SectionRequiredMixin, FormView):  # type: ignore[type-arg]
    """The settings of a proxy; a service the app has no settings for shows what it has."""

    section = Section.SERVERS

    template_name = "protocols/service_settings.html"

    @cached_property
    def service(self) -> ServiceInstance:
        # Looked up after the role check of the view, so a guest cannot tell which services exist.
        return get_object_or_404(
            _services(self.zone),
            pk=self.kwargs["pk"],
        )

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if self.service.container not in SERVICE_SETTINGS_FORMS:
            return _settings_info(request, self.service)
        return super().get(request, *args, **kwargs)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if self.service.container not in SERVICE_SETTINGS_FORMS:
            raise Http404
        return super().post(request, *args, **kwargs)

    def get_form_class(self) -> type[ServiceSettingsForm]:
        return SERVICE_SETTINGS_FORMS[self.service.container]

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "service": self.service}

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(service=self.service, **kwargs)

    def form_valid(self, form: ServiceSettingsForm) -> HttpResponse:
        operation = form.start(self.member, ip=self.client_ip)
        return redirect("operations:detail", pk=operation.pk)


def _settings_info(
    request: HttpRequest, subject: ProtocolInstance | ServiceInstance
) -> HttpResponse:
    """The settings page of a container the app has nothing to change in."""
    context = {"subject": subject, "info": SettingsInfo.of(subject)}
    return render(request, "protocols/settings_info.html", context)


class ServiceRemoveView(SectionRequiredMixin, View):
    """Removal of a service container, confirmed on the server card."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        # Looked up after the role check of the view, so a guest cannot tell which services exist.
        service = get_object_or_404(
            _services(self.zone),
            pk=pk,
        )
        operation = start_service_removal(service, self.member, ip=self.client_ip)
        return redirect("operations:detail", pk=operation.pk)


class ServiceSecretView(SectionRequiredMixin, View):
    """A new secret of a Telegram proxy, confirmed on the server card: old links stop working."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        service = get_object_or_404(
            _services(self.zone),
            pk=pk,
        )
        if not renews_secret(service):
            raise Http404
        operation = start_secret_renewal(service, self.member, ip=self.client_ip)
        return redirect("operations:detail", pk=operation.pk)


class _ServiceRevealView(SectionRequiredMixin, View):
    """A secret of a service, sent only on request, not cached; every look goes to the journal."""

    section = Section.SERVERS

    action: str
    template_name: str

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        service = get_object_or_404(
            _services(self.zone),
            pk=pk,
        )
        context = self.reveal(service)
        if context is None:
            raise Http404
        audit.log_request(request, self.action, service)
        response = render(request, self.template_name, context)
        add_never_cache_headers(response)
        return response

    def reveal(self, service: ServiceInstance) -> dict[str, Any] | None:
        """The template context with the secret; None if the service has nothing to show."""
        raise NotImplementedError


class ServicePasswordView(_ServiceRevealView):
    """The password of SFTP or SOCKS5."""

    action = PASSWORD_VIEW
    template_name = "protocols/_password.html"

    def reveal(self, service: ServiceInstance) -> dict[str, Any] | None:
        if not ((service.is_sftp or service.is_socks5) and service.secret):
            return None
        return {"password": service.secret}


class ServiceLinksView(_ServiceRevealView):
    """The tg:// and t.me links of a Telegram proxy: they carry its secret."""

    action = LINKS_VIEW
    template_name = "protocols/_proxy_links.html"

    def reveal(self, service: ServiceInstance) -> dict[str, Any] | None:
        links = service.links
        return {"links": links} if links else None


class ServiceLogDeletionView(SectionRequiredMixin, View):
    """Deletes the 3proxy.log the app left in SOCKS5, confirmed on the server card."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        service = get_object_or_404(
            _services(self.zone),
            pk=pk,
        )
        if not service.is_socks5:
            raise Http404
        operation = start_log_deletion(service, self.member, ip=self.client_ip)
        return redirect("operations:detail", pk=operation.pk)


class DnsRebuildView(SectionRequiredMixin, View):
    """Rebuilds AmneziaDNS the app installed with the panel's config, confirmed on the card."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        service = get_object_or_404(
            _services(self.zone),
            pk=pk,
        )
        if not service.is_dns:
            raise Http404
        operation = start_dns_rebuild(service, self.member, ip=self.client_ip)
        return redirect("operations:detail", pk=operation.pk)


class ServiceActionView(SectionRequiredMixin, View):
    """Restart or stop a service container, or read its log; the operation page shows it."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int, action: str) -> HttpResponse:
        if action not in _SERVICE_ACTION_KINDS:
            raise Http404
        service = get_object_or_404(
            _services(self.zone),
            pk=pk,
        )
        operation = start_service_action(_SERVICE_ACTION_KINDS[action], service, self.member)
        return redirect("operations:detail", pk=operation.pk)


class ReaddressView(SectionRequiredMixin, FormView):  # type: ignore[type-arg]
    """After the server address changes: keys move to the new address, their owners are told."""

    section = Section.SERVERS

    template_name = "protocols/readdress.html"
    form_class = ReaddressForm

    @cached_property
    def server(self) -> Server:
        return get_object_or_404(self.zone.servers(), pk=self.kwargs["server_pk"])

    @cached_property
    def readdress(self) -> ServerReaddress:
        return ServerReaddress(self.server)

    @cached_property
    def ipsec(self) -> IpsecIdentity | None:
        return IpsecIdentity.of(self.server)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(
            server=self.server,
            outdated=len(self.readdress.outdated),
            imported=len(self.readdress.imported),
            ipsec=self.ipsec,
            **kwargs,
        )

    def form_valid(self, form: ReaddressForm) -> HttpResponse:
        ip = self.client_ip
        result = self.readdress.apply(self.member, reissue=form.cleaned_data["reissue"], ip=ip)
        if self.ipsec is not None and form.cleaned_data["ipsec"]:
            start_reissue(self.ipsec.instance, self.member, reissue=False, ip=ip)
        messages.success(
            self.request,
            _(
                "Keys moved to the new address: %(moved)d, reissued: %(reissued)d. "
                "Owners notified: %(owners)d."
            )
            % {"moved": result.moved, "reissued": result.reissued, "owners": result.owners},
        )
        return redirect("servers:detail", pk=self.server.pk)


class AwgUpgradeView(SectionRequiredMixin, InstanceMixin, View):
    """Start the move of AmneziaWG 2.0 to 3.1; the operation page shows how it goes."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        if not can_upgrade(self.instance):
            raise Http404
        form = AwgUpgradeForm(request.POST)
        data = form.cleaned_data if form.is_valid() else {}
        operation = start_awg_upgrade(
            self.instance,
            self.member,
            reissue=data.get("reissue", False),
            masking=data.get("masking", ""),
            ip=self.client_ip,
        )
        return redirect("operations:detail", pk=operation.pk)


class AwgMigrationView(SectionRequiredMixin, InstanceMixin, View):
    """Start the move of the clients of a legacy AmneziaWG to amnezia-awg2 on 3.1."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        if not can_migrate(self.instance):
            raise Http404
        state = MigrationState.of(self.instance)
        port = state.port or 0
        if state.port is not None:
            form = AwgMigrationForm(request.POST, server=self.instance.server)
            if not form.is_valid():
                for error in form.errors.get_json_data().get("port", []):
                    messages.error(request, error["message"])
                return redirect("servers:detail", pk=self.instance.server_id)
            port = form.cleaned_data["port"]
        operation = start_migration(self.instance, self.member, port=port, ip=self.client_ip)
        return redirect("operations:detail", pk=operation.pk)


class AwgMigrationFinishView(SectionRequiredMixin, InstanceMixin, View):
    """Finish the move: the old container is removed, its keys are revoked."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        if not can_migrate(self.instance) or not MigrationState.of(self.instance).can_finish:
            raise Http404
        operation = start_finish(self.instance, self.member, ip=self.client_ip)
        return redirect("operations:detail", pk=operation.pk)


class XrayProfilesView(SectionRequiredMixin, InstanceMixin, View):
    """Prepare every imported XRay key for showing by one operation."""

    section = Section.SERVERS

    containers = (XRAY_CONTAINER,)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        operation = prepare_all(self.instance.source, self.member)
        return redirect("operations:detail", pk=operation.pk)


class IpsecReissueView(SectionRequiredMixin, View):
    """Start reissuing the IPsec server certificate for the server IP."""

    section = Section.SERVERS

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        instances = self.zone.narrow(ProtocolInstance.objects.all(), server="server")
        instance = get_object_or_404(instances, pk=pk, container_removed_at__isnull=True)
        identity = IpsecIdentity.of(instance.server)
        if identity is None or identity.instance != instance:
            raise Http404
        form = IpsecReissueForm(request.POST)
        reissue = form.is_valid() and form.cleaned_data["reissue"]
        start_reissue(instance, self.member, reissue=reissue, ip=self.client_ip)
        return redirect("servers:detail", pk=instance.server_id)
