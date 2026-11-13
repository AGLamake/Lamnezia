"""Operation kinds of the protocols app; registered in `ProtocolsConfig.ready()`."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace

from django.db import models, transaction
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from amnezia_core.containers import AWG2, WG_CONTAINERS
from apps.accounts.models import User
from apps.keys.models import AccessKey
from apps.operations.services.context import OperationContext
from apps.operations.services.registry import operations
from apps.operations.services.subjects import subjects
from apps.protocols.models import InstanceOrigin, ProtocolInstance, ServiceInstance
from apps.protocols.services import offload, speed_test
from apps.protocols.services.awg_migration import (
    FINISH_KIND,
    MIGRATE_KIND,
    AwgMigrationRun,
    LegacyRetirement,
)
from apps.protocols.services.awg_settings import (
    SETTINGS_KIND,
    AwgReconfiguration,
    AwgSettings,
    InstanceSettings,
)
from apps.protocols.services.awg_upgrade import UPGRADE_KIND
from apps.protocols.services.dns_rebuild import REBUILD_DNS_KIND, DnsRebuild
from apps.protocols.services.importer import IMPORT_KIND, ServerImporter
from apps.protocols.services.installation import INSTALL_KIND
from apps.protocols.services.installer import installers
from apps.protocols.services.ipsec_reissue import REISSUE_KIND, IpsecProfiles, ServerCertificate
from apps.protocols.services.lifecycle import (
    LOGS_KIND,
    REMOVE_KIND,
    RESTART_KIND,
    RESTART_SERVICE_KIND,
    SERVICE_LOGS_KIND,
    STOP_KIND,
    STOP_SERVICE_KIND,
    ContainerControl,
    ContainerLifecycle,
    RemovedInstance,
)
from apps.protocols.services.offload import OffloadFix
from apps.protocols.services.openvpn_settings import (
    OPENVPN_SETTINGS_KIND,
    OpenVpnPanelSettings,
    OpenVpnReconfiguration,
)
from apps.protocols.services.proxy_log import DELETE_LOG_KIND, ProxyLogDeletion
from apps.protocols.services.proxy_secret import RENEW_SECRET_KIND, SecretRenewal
from apps.protocols.services.service_removal import REMOVE_SERVICE_KIND, ServiceRemoval
from apps.protocols.services.service_settings import (
    SERVICE_SETTINGS_KIND,
    ServiceReconfiguration,
    ServiceSettings,
)
from apps.protocols.services.settings_change import ContainerSettings
from apps.protocols.services.speed_test import SpeedTest, SpeedTestPlan
from apps.protocols.services.xray_settings import (
    XRAY_SETTINGS_KIND,
    XrayPanelSettings,
    XrayReconfiguration,
)
from apps.servers.models import Server
from apps.servers.services.connection import connect
from apps.ssh.services import RemoteHost


def _server_page(model: type[models.Model]) -> Callable[[int], str | None]:
    """A protocol or a service has no page of its own: the operation leads to its server."""

    def resolve(pk: int) -> str | None:
        server_id = model._default_manager.filter(pk=pk).values_list("server_id", flat=True).first()
        return reverse("servers:detail", args=[server_id]) if server_id else None

    return resolve


subjects.register("instance_id", _server_page(ProtocolInstance), priority=5)
subjects.register("service_id", _server_page(ServiceInstance), priority=5)


@operations.register(IMPORT_KIND, gettext_lazy("Import from the server"))
def import_server(ctx: OperationContext) -> None:
    server = Server.objects.get(pk=ctx.params["server_id"])
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(server))
        ServerImporter(server, host, ctx).run()


@operations.register(INSTALL_KIND, gettext_lazy("Install on the server"))
def install(ctx: OperationContext) -> None:
    """Install the container, then read the server back so the panel knows what is there.

    The TSO/GSO workaround of a WireGuard-family container comes last: the protocol is already
    in the panel if it fails, and the server card turns it on again.
    """
    server = Server.objects.get(pk=ctx.params["server_id"])
    container = ctx.params["container"]
    installer = installers.get(container)
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(server))
        installer.from_params(host, ctx, ctx.params).run()
        ServerImporter(server, host, ctx, origin=InstanceOrigin.INSTALLED).run()
        if ctx.params.get("offload") and container in WG_CONTAINERS:
            OffloadFix(server, host, ctx).enable()


@operations.register(offload.CHECK_KIND, gettext_lazy("Check TSO/GSO"))
def check_offload(ctx: OperationContext) -> None:
    with _offload(ctx) as fix:
        fix.check()


@operations.register(offload.ENABLE_KIND, gettext_lazy("Turn the TSO/GSO workaround on"))
def enable_offload(ctx: OperationContext) -> None:
    with _offload(ctx) as fix:
        fix.enable()


@operations.register(offload.DISABLE_KIND, gettext_lazy("Turn the TSO/GSO workaround off"))
def disable_offload(ctx: OperationContext) -> None:
    with _offload(ctx) as fix:
        fix.disable()


@operations.register(speed_test.START_KIND, gettext_lazy("Start the port speed test"))
def start_speed_test(ctx: OperationContext) -> None:
    with _speed_test(ctx) as test:
        test.start(SpeedTestPlan.from_params(ctx.params))


@operations.register(speed_test.STOP_KIND, gettext_lazy("Stop the port speed test"))
def stop_speed_test(ctx: OperationContext) -> None:
    with _speed_test(ctx) as test:
        test.stop()


@operations.register(SETTINGS_KIND, gettext_lazy("Change the protocol settings"))
def change_settings(ctx: OperationContext) -> None:
    """A DNS change only updates the profiles; the server is touched for the port, the AWG and
    the container log. Owners hear of it only when their profiles change.
    """
    instance = _instance(ctx)
    current = AwgSettings.of(instance)
    settings = AwgSettings.from_params(ctx.params).within(current)
    if settings.touches_server(current):
        with ExitStack() as stack:
            with ctx.step(_("Connect to the server")):
                host = stack.enter_context(connect(instance.server))
            AwgReconfiguration(host, ctx, WG_CONTAINERS[instance.container]).apply(settings)
    panel = InstanceSettings(instance, ctx)
    with ctx.step(_("Update the keys")):
        keys = panel.save(settings)
        _finish_keys(ctx, panel, keys, tell=settings.changes_profiles(current))


@operations.register(UPGRADE_KIND, gettext_lazy("Move AmneziaWG to version 3.1"))
def upgrade_awg(ctx: OperationContext) -> None:
    """The server keeps its keys and clients; every client imports the key again."""
    instance = _instance(ctx)
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(instance.server))
        config = AwgReconfiguration(host, ctx, AWG2).upgrade(instance.port)
    panel = InstanceSettings(instance, ctx)
    with ctx.step(_("Update the keys")):
        keys = panel.save(replace(AwgSettings.of(instance), params=config.awg_params))
        _finish_keys(ctx, panel, keys, tell=True)


@operations.register(MIGRATE_KIND, gettext_lazy("Move the clients to AmneziaWG 3.1"))
def migrate_awg(ctx: OperationContext) -> None:
    """New keys on amnezia-awg2, installed if needed; the old container keeps working."""
    legacy = _instance(ctx)
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(legacy.server))
        AwgMigrationRun(legacy, host, ctx).run(int(ctx.params["port"]))


@operations.register(FINISH_KIND, gettext_lazy("Finish the move to AmneziaWG 3.1"))
def finish_awg_migration(ctx: OperationContext) -> None:
    """The old container is removed and its keys revoked."""
    legacy = _instance(ctx)
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(legacy.server))
        LegacyRetirement(legacy, host, ctx).run()


@operations.register(XRAY_SETTINGS_KIND, gettext_lazy("Change the XRay settings"))
def change_xray_settings(ctx: OperationContext) -> None:
    """server.json is rewritten for the port, site, SNI and flow; a new port or container log
    moves the data volume into a new container. The fingerprint only changes the profiles.
    """
    instance = _instance(ctx)
    settings = XrayPanelSettings.from_params(ctx.params)
    _change_container_settings(
        ctx,
        instance,
        settings,
        XrayPanelSettings.of(instance),
        lambda host: XrayReconfiguration(host, ctx, instance.source).apply(settings),
    )


@operations.register(OPENVPN_SETTINGS_KIND, gettext_lazy("Change the OpenVPN settings"))
def change_openvpn_settings(ctx: OperationContext) -> None:
    """A new container with the certificate authority of the old one and a new server.conf."""
    instance = _instance(ctx)
    settings = OpenVpnPanelSettings.from_params(ctx.params)
    _change_container_settings(
        ctx,
        instance,
        settings,
        OpenVpnPanelSettings.of(instance),
        lambda host: OpenVpnReconfiguration(host, ctx, instance.server.host).apply(settings),
    )


def _change_container_settings[T: ContainerSettings](
    ctx: OperationContext,
    instance: ProtocolInstance,
    settings: T,
    current: T,
    reconfigure: Callable[[RemoteHost], None],
) -> None:
    """Change the server if needed, then the instance and the key profiles."""
    if settings.touches_server(current):
        with ExitStack() as stack:
            with ctx.step(_("Connect to the server")):
                host = stack.enter_context(connect(instance.server))
            reconfigure(host)
    panel = InstanceSettings(instance, ctx)
    with ctx.step(_("Update the keys")):
        with transaction.atomic():
            settings.store(instance)
            keys = panel.update_profiles(settings.updated_profile)
        _finish_keys(ctx, panel, keys, tell=settings.changes_profiles(current))


def _finish_keys(
    ctx: OperationContext, panel: InstanceSettings, keys: list[AccessKey], *, tell: bool
) -> None:
    """Reissue the imported keys if asked, and tell the owners whose profiles changed."""
    if ctx.params["reissue"]:
        panel.reissue_imported(User.objects.filter(pk=ctx.params["user_id"]).first())
    if tell:
        panel.tell_owners(keys)


@operations.register(REISSUE_KIND, gettext_lazy("Reissue the IPsec server certificate"))
def reissue_ipsec_certificate(ctx: OperationContext) -> None:
    """A certificate for the new server IP; the profiles name it and the owners are told."""
    instance = _instance(ctx)
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(instance.server))
        server_ip = ServerCertificate(host, ctx).reissue(instance.server.host)
    panel = InstanceSettings(instance, ctx)
    with ctx.step(_("Update the keys")):
        keys = IpsecProfiles(instance, ctx).save(server_ip)
        if ctx.params["reissue"]:
            actor = User.objects.filter(pk=ctx.params["user_id"]).first()
            keys += panel.reissue_imported(actor)
        panel.tell_owners(keys)


@operations.register(RESTART_KIND, gettext_lazy("Restart the container"))
def restart_container(ctx: OperationContext) -> None:
    with _lifecycle(ctx) as lifecycle:
        lifecycle.restart()


@operations.register(STOP_KIND, gettext_lazy("Stop the container"))
def stop_container(ctx: OperationContext) -> None:
    with _lifecycle(ctx) as lifecycle:
        lifecycle.stop()


@operations.register(REMOVE_KIND, gettext_lazy("Remove the container"))
def remove_container(ctx: OperationContext) -> None:
    """A container removed before, with its backup kept, is not looked for on the server again."""
    instance = _instance(ctx)
    delete_backups = bool(ctx.params.get("delete_backups"))
    if not instance.removed:
        with _lifecycle(ctx, instance) as lifecycle:
            lifecycle.remove(keep_keys=not delete_backups)
    removed = RemovedInstance(instance, ctx)
    if delete_backups:
        with ctx.step(_("Revoke the keys and delete the backup")):
            removed.forget()
    else:
        with ctx.step(_("Keep the keys and the backup")):
            removed.keep()


@operations.register(REMOVE_SERVICE_KIND, gettext_lazy("Remove the service"))
def remove_service(ctx: OperationContext) -> None:
    service = ServiceInstance.objects.select_related("server").get(pk=ctx.params["service_id"])
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(service.server))
        ServiceRemoval(host, service, ctx).run()


@operations.register(RENEW_SECRET_KIND, gettext_lazy("New secret of the proxy"))
def renew_proxy_secret(ctx: OperationContext) -> None:
    service = ServiceInstance.objects.select_related("server").get(pk=ctx.params["service_id"])
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(service.server))
        SecretRenewal(host, service, ctx).run()


@operations.register(SERVICE_SETTINGS_KIND, gettext_lazy("Change the service settings"))
def change_service_settings(ctx: OperationContext) -> None:
    service = ServiceInstance.objects.select_related("server").get(pk=ctx.params["service_id"])
    settings = ServiceSettings.from_params(ctx.params)
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(service.server))
        ServiceReconfiguration(host, service, ctx).apply(settings)


@operations.register(DELETE_LOG_KIND, gettext_lazy("Delete the SOCKS5 log"))
def delete_proxy_log(ctx: OperationContext) -> None:
    service = ServiceInstance.objects.select_related("server").get(pk=ctx.params["service_id"])
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(service.server))
        ProxyLogDeletion(host, service, ctx).run()


@operations.register(REBUILD_DNS_KIND, gettext_lazy("Rebuild AmneziaDNS"))
def rebuild_dns(ctx: OperationContext) -> None:
    service = ServiceInstance.objects.select_related("server").get(pk=ctx.params["service_id"])
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(service.server))
        DnsRebuild(host, service, ctx).run()


@operations.register(LOGS_KIND, gettext_lazy("Container log"))
def container_logs(ctx: OperationContext) -> None:
    with _lifecycle(ctx) as lifecycle:
        lifecycle.show_log()


@operations.register(RESTART_SERVICE_KIND, gettext_lazy("Restart the service"))
def restart_service(ctx: OperationContext) -> None:
    with _service_control(ctx) as control:
        control.restart()


@operations.register(STOP_SERVICE_KIND, gettext_lazy("Stop the service"))
def stop_service(ctx: OperationContext) -> None:
    with _service_control(ctx) as control:
        control.stop()


@operations.register(SERVICE_LOGS_KIND, gettext_lazy("Service log"))
def service_logs(ctx: OperationContext) -> None:
    with _service_control(ctx) as control:
        control.show_log()


def _instance(ctx: OperationContext) -> ProtocolInstance:
    return ProtocolInstance.objects.select_related("server").get(pk=ctx.params["instance_id"])


@contextmanager
def _lifecycle(
    ctx: OperationContext, instance: ProtocolInstance | None = None
) -> Iterator[ContainerLifecycle]:
    instance = instance or _instance(ctx)
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(instance.server))
        yield ContainerLifecycle(host, instance, ctx)


@contextmanager
def _offload(ctx: OperationContext) -> Iterator[OffloadFix]:
    server = Server.objects.get(pk=ctx.params["server_id"])
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(server))
        yield OffloadFix(server, host, ctx)


@contextmanager
def _speed_test(ctx: OperationContext) -> Iterator[SpeedTest]:
    server = Server.objects.get(pk=ctx.params["server_id"])
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(server))
        yield SpeedTest(host, ctx)


@contextmanager
def _service_control(ctx: OperationContext) -> Iterator[ContainerControl]:
    service = ServiceInstance.objects.select_related("server").get(pk=ctx.params["service_id"])
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(service.server))
        yield ContainerControl(host, service.container, ctx)
