"""Installing a protocol or a service from the server card: what can go where, and the start.

The operation itself runs an installer of the registry (services/installer.py) and then reads
the server back as the import does, so the new container shows up in the panel at once.
"""

from dataclasses import dataclass

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from amnezia_core.proxy_options import TelegramProxyOptions
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation
from apps.operations.services.launcher import launch
from apps.protocols.catalog import CATALOG, ContainerGroup
from apps.protocols.models import ProtocolInstance, ServiceInstance
from apps.protocols.services.installer import InstallerClass, installers
from apps.servers.models import Server

INSTALL_KIND = "protocols.install"


@dataclass(frozen=True)
class InstallChoice:
    """One installer on the install page; `reason` says why it cannot be installed now.

    `port` is the one an empty port field gives on this server.
    """

    installer: InstallerClass
    port: int
    reason: Promise | None = None

    @property
    def available(self) -> bool:
        return self.reason is None

    # Templates call a class they are given, so they get the installer's values from here.
    @property
    def title(self) -> str:
        return str(self.installer.title)

    @property
    def container(self) -> str:
        return self.installer.container

    @property
    def group(self) -> ContainerGroup:
        kind = CATALOG.get(self.installer.container)
        return kind.group if kind else ContainerGroup.SERVICES

    @property
    def description(self) -> str:
        kind = CATALOG.get(self.installer.container)
        return str(kind.description) if kind else ""

    @property
    def ports(self) -> str:
        if not self.installer.publishes_port:
            return "—"
        ports = sorted(self.installer.published_ports(self.port))
        return ", ".join(f"{port}/{transport}" for port, transport in ports)


def install_choices(server: Server) -> list[InstallChoice]:
    """Every registered installer, VPN protocols first, with what keeps it from this server."""
    instances = {
        instance.container: instance for instance in ProtocolInstance.objects.filter(server=server)
    }
    on_server = {container["name"] for container in server.containers}
    on_server |= set(
        ServiceInstance.objects.filter(server=server).values_list("container", flat=True)
    )
    taken = used_ports(server)
    choices = []
    for installer in installers:
        instance = instances.get(installer.container)
        reason: Promise | None = None
        if instance and instance.removed:
            reason = _("Removed with its backup kept: restore it from the backup instead.")
        elif instance or installer.container in on_server:
            reason = _("Already on the server.")
        choices.append(InstallChoice(installer, installer.suggested_port(taken), reason))
    groups = list(ContainerGroup)
    return sorted(choices, key=lambda choice: groups.index(choice.group))


def used_ports(server: Server) -> set[tuple[int, str]]:
    """(port, transport) the panel knows to be taken on the server."""
    ports = {
        (int(container["port"]), str(container["transport"]))
        for container in server.containers
        if container.get("port")
    }
    for instance in ProtocolInstance.objects.filter(server=server, container_removed_at=None):
        installer = installers.find(instance.container)
        if installer:
            ports |= installer.ports_of(instance.port, instance.params)
        else:
            ports.add((instance.port, "udp"))
    if server.probe_port:
        ports.add((server.probe_port, "udp"))
    return ports


@dataclass(frozen=True)
class InstallRequest:
    """What the administrator chose on the install page."""

    installer: InstallerClass
    port: int
    logs: bool = False
    # The masking site of an installer that asks for one.
    site: str = ""
    # The domain and the certificate e-mail of an installer that asks for them.
    domain: str = ""
    email: str = ""
    # The TSO/GSO workaround of a WireGuard-family container (services/offload.py).
    offload: bool = False
    # The settings of MTProxy and Telemt.
    proxy: TelegramProxyOptions | None = None


def start_install(
    server: Server, request: InstallRequest, user: User, *, ip: str | None = None
) -> Operation:
    container = request.installer.container
    params = {
        "server_id": server.pk,
        "container": container,
        "port": request.port,
        "logs": request.logs,
        # OpenVPN gives the server address to the container.
        "server_host": server.host,
    }
    if request.site:
        params["site"] = request.site
    if request.offload:
        params["offload"] = True
    if request.domain:
        params["hostname"] = request.domain
        params["email"] = request.email
    if request.proxy:
        params["proxy"] = request.proxy.to_params()
    operation = launch(INSTALL_KIND, params, user=user, subject=server.name)
    data = {"container": container, "port": request.port, "operation": operation.pk}
    audit.log(INSTALL_KIND, actor=user, target=server, data=data, ip=ip)
    return operation
