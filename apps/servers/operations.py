"""Operation kinds of the servers app; registered in `ServersConfig.ready()`."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.operations.services.context import OperationContext
from apps.operations.services.registry import operations
from apps.operations.services.subjects import subjects
from apps.protocols.services.lifecycle import LostInterfaceRestart
from apps.servers.models import Server, ServerAddress
from apps.servers.services import interface_watch, probe
from apps.servers.services.addresses import CHECK_ADDRESS_KIND, AddressCheck
from apps.servers.services.check import CHECK_KIND, ServerCheck
from apps.servers.services.connection import connect

subjects.register("server_id", lambda pk: reverse("servers:detail", args=[pk]))


@operations.register(CHECK_KIND, gettext_lazy("Check server"))
def check_server(ctx: OperationContext) -> None:
    server = Server.objects.get(pk=ctx.params["server_id"])
    try:
        with ExitStack() as stack:
            with ctx.step(_("Connect to the server")):
                host = stack.enter_context(connect(server))
                ctx.log(f"{server.username}@{server.host}:{server.port}")
            facts = ServerCheck(host, ctx, settings.SERVER_MIN_FREE_DISK).run()
    except Exception:
        server.record_failed_check()
        raise
    server.record_check(facts)


@operations.register(CHECK_ADDRESS_KIND, gettext_lazy("Check another server address"))
def check_address(ctx: OperationContext) -> None:
    addresses = ServerAddress.objects.select_related("server")
    address = addresses.filter(pk=ctx.params["address_id"]).first()
    if address is None:  # removed before the check ran
        return
    with ctx.step(_("Compare the SSH key at the address with the server's")):
        AddressCheck(address, ctx).run()


@operations.register(probe.INSTALL_KIND, gettext_lazy("Install the reachability probe"))
def install_probe(ctx: OperationContext) -> None:
    with _probe_setup(ctx) as setup:
        setup.install(int(ctx.params["port"]))


@operations.register(probe.REMOVE_KIND, gettext_lazy("Remove the reachability probe"))
def remove_probe(ctx: OperationContext) -> None:
    with _probe_setup(ctx) as setup:
        setup.remove()


@operations.register(
    interface_watch.RESTART_KIND, gettext_lazy("Restart a container that lost its VPN interface")
)
def restart_lost_interface(ctx: OperationContext) -> None:
    server = Server.objects.get(pk=ctx.params["server_id"])
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(server))
        LostInterfaceRestart(host, ctx.params["container"], ctx).restart()


@contextmanager
def _probe_setup(ctx: OperationContext) -> Iterator[probe.ProbeSetup]:
    server = Server.objects.get(pk=ctx.params["server_id"])
    with ExitStack() as stack:
        with ctx.step(_("Connect to the server")):
            host = stack.enter_context(connect(server))
        yield probe.ProbeSetup(server, host, ctx, probe.StunPing())
