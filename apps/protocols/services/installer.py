"""Installing an Amnezia container on a server, step for step as the Amnezia app does it.

Follows upstream InstallController::setupContainer. A protocol or a service is one subclass of
`ContainerInstaller` registered with `installers.register`; the install page lists them all.
"""

import socket
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from typing import Any, ClassVar, Protocol, Self

from django.utils.functional import Promise
from django.utils.translation import gettext as _

from amnezia_core.script_vars import container_variables, substitute
from apps.operations.services.context import OperationContext
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.container import ProtocolContainer, Transport, upstream_file
from apps.protocols.services.host_setup import HostSetup
from apps.protocols.services.ports import LOW_PORTS, free_ports, is_high
from apps.ssh.services import RemoteHost

START_SCRIPT = "/opt/amnezia/start.sh"


def ipv4_addresses(name: str) -> list[str]:
    """The IPv4 addresses DNS gives for `name`, the first one first; raises OSError."""
    try:
        found = socket.getaddrinfo(name, None, socket.AF_INET)
    except UnicodeError as error:
        raise OSError(str(error)) from error
    return list(dict.fromkeys(str(entry[4][0]) for entry in found))


def resolve_server_ip(ctx: OperationContext, server_host: str) -> str:
    """The IPv4 address of a server given by name, looked up as the app does it."""
    with ctx.step(_("Find the server address")):
        try:
            server_ip = ipv4_addresses(server_host)[0]
        except (OSError, IndexError) as error:
            raise SetupError(SetupErrorCode.HOST_NOT_RESOLVED, server_host) from error
        ctx.log(_("%(host)s is %(ip)s.") % {"host": server_host, "ip": server_ip})
        return server_ip


class ContainerSeed(Protocol):
    """Files put into a new container before it is configured: a restore from a backup."""

    def put(self, docker: ProtocolContainer) -> None: ...

    def unfinished(self, docker: ProtocolContainer) -> bool:
        """Whether the container is one an earlier seed did not finish: it is made anew."""
        ...


class ContainerGuard(Protocol):
    """Checks a container already on the server before the installer starts it: a restore.

    Raises when the container belongs to other keys, so it is never started or configured.
    """

    def check(self, docker: ProtocolContainer) -> None: ...


class ContainerInstaller[ResultT](ABC):
    """Builds, runs and configures one Amnezia container; a subclass is one container type.

    A subclass sets the class attributes and implements `run_arguments` and `configure`;
    `start` runs upstream start.sh unless the container needs more. An existing container is
    never rebuilt, so a retry, or a server set up in the Amnezia app, keeps its keys and clients.
    """

    container: ClassVar[str]
    # A service name the app translates is a lazy translation.
    title: ClassVar[str | Promise]
    # Upstream server_scripts/<folder>/, copied to apps/protocols/upstream/<folder>/.
    folder: ClassVar[str]
    transport: ClassVar[Transport]
    default_port: ClassVar[int]
    # Upstream installs conntrack for the Telegram proxies.
    conntrack: ClassVar[bool] = False
    # The container keeps its identity in the `<container>-data` volume, removed with it.
    data_volume: ClassVar[bool] = False
    # The fields of the install form, besides the port and the log, the installer reads from
    # its params (forms.InstallForm): the form shows and checks only these.
    install_fields: ClassVar[tuple[str, ...]] = ()
    # The container publishes `default_port` or the chosen one on the server; AmneziaDNS and the
    # Tor site publish none, so their port is neither asked for nor checked.
    publishes_port: ClassVar[bool] = True
    # The ports are those of upstream run_container.sh and cannot be chosen (IPsec).
    fixed_port: ClassVar[bool] = False
    # An empty port field gives a free port up to 9999 instead of `default_port` (AmneziaWG,
    # services/ports.py).
    low_port: ClassVar[bool] = False
    # The container joins amnezia-dns-net, as the run_container.sh of the VPN protocols do.
    joins_network: ClassVar[bool] = True

    def __init__(
        self,
        host: RemoteHost,
        ctx: OperationContext,
        docker: ProtocolContainer | None = None,
        *,
        port: int,
        logs: bool = False,
    ) -> None:
        self._host = host
        self._ctx = ctx
        self._docker = docker or ProtocolContainer(host, ctx, self.container)
        self.port = port
        self.logs = logs
        # Set by a restore: the saved files go in before `configure`, which then keeps them.
        self.seed: ContainerSeed | None = None
        # Set by a restore: a container already on the server is checked before it starts.
        self.guard: ContainerGuard | None = None

    @classmethod
    def from_params(
        cls, host: RemoteHost, ctx: OperationContext, params: Mapping[str, Any]
    ) -> Self:
        """The installer for what the install form asked: `port` and `logs`."""
        return cls(host, ctx, port=int(params["port"]), logs=bool(params["logs"]))

    @classmethod
    def published_ports(cls, port: int) -> set[tuple[int, str]]:
        """(port, transport) the container publishes on the server."""
        return {(port, str(cls.transport))} if cls.publishes_port else set()

    @classmethod
    def ports_of(cls, port: int, params: Mapping[str, Any]) -> set[tuple[int, str]]:
        """(port, transport) a container on the server publishes, given its instance params."""
        return cls.published_ports(port)

    @classmethod
    def suggested_port(cls, taken: set[tuple[int, str]]) -> int:
        """The port of an empty port field, given the (port, transport) taken on the server."""
        if cls.low_port:
            free = free_ports(LOW_PORTS, taken, (str(cls.transport),))
            if free:
                return free[0]
        return cls.default_port

    @classmethod
    def low_port_advice(cls, taken: set[tuple[int, str]]) -> int | None:
        """A free port up to 9999 to offer for a UDP protocol that keeps the app's high port.

        WireGuard keeps 51820 of the Amnezia app on an empty port field; the install form only
        suggests a low port to enter instead.
        """
        choosable = cls.publishes_port and not cls.fixed_port
        if cls.low_port or not choosable or cls.transport != Transport.UDP:
            return None
        if not is_high(cls.default_port):
            return None
        free = free_ports(LOW_PORTS, taken, (str(cls.transport),))
        return free[0] if free else None

    def run(self) -> ResultT:
        host_setup = HostSetup(self._host, self._ctx)
        host_setup.check_os()
        self.check_server()
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            host_setup.prepare()
            if self.conntrack:
                host_setup.install_conntrack()
            self._create_container()
            if self.seed:
                self.seed.put(self._docker)
            with self._ctx.step(_("Configure %(title)s") % {"title": self.title}):
                result = self.configure()
            with self._ctx.step(_("Start %(title)s") % {"title": self.title}):
                self.start(result)
        return result

    def check_server(self) -> None:  # noqa: B027 - an optional hook
        """Refuse a server the container cannot run on, before anything is changed."""

    @abstractmethod
    def run_arguments(self) -> list[str]:
        """The `docker run` command of upstream run_container.sh."""

    @abstractmethod
    def configure(self) -> ResultT:
        """Write the configuration in the running container; keep the one already there."""

    def start(self, result: ResultT) -> None:
        """Upload upstream start.sh and run it in the background, as startupContainerWorker."""
        self.write_start_script()
        command = f"chmod a+x {START_SCRIPT} && {START_SCRIPT}"
        self._host.docker("exec", "-d", self.container, "sh", "-c", command)

    def write_start_script(self) -> None:
        """Upload upstream start.sh with the settings of the installer."""
        script = self.script("start.sh")
        self._docker.shell.write_file_atomic(START_SCRIPT, script.encode(), mode=0o755)

    def script_variables(self) -> dict[str, str]:
        """Values for the `$NAME` placeholders of the upstream scripts of the container."""
        return container_variables(self.container)

    def script(self, name: str) -> str:
        """An upstream script of the container with its placeholders filled in."""
        return substitute(upstream_file(f"{self.folder}/{name}"), self.script_variables())

    def _create_container(self) -> None:
        state = self._docker.state()
        if state and self.seed and self.seed.unfinished(self._docker):
            with self._ctx.step(_("Remove the container of the unfinished restore")):
                self._docker.remove(volumes=True)
            state = ""
        if state:
            if self.guard:
                with self._ctx.step(_("Check the container on the server")):
                    self.guard.check(self._docker)
            with self._ctx.step(_("Start the container")):
                self._ctx.log(
                    _("The container %(name)s already exists and is kept.")
                    % {"name": self.container}
                )
                if state != "running":
                    self._host.docker("start", self.container)
                self._join_network()
            return
        if self.publishes_port:
            with self._ctx.step(_("Check the port")):
                for port, transport in sorted(self.published_ports(self.port)):
                    self._docker.check_port(port, Transport(transport))
        with self._ctx.step(_("Build the container image")):
            self._docker.build(upstream_file(f"{self.folder}/Dockerfile"))
        with self._ctx.step(_("Start the container")):
            self._docker.run_container(self.run_arguments())
            self._join_network()

    def _join_network(self) -> None:
        if self.joins_network:
            self._docker.connect_network()


type InstallerClass = type[ContainerInstaller[Any]]


class InstallerRegistry:
    """Installers by container name, in the order they are registered.

    Usage::

        @installers.register
        class XrayInstaller(ContainerInstaller[None]): ...
    """

    def __init__(self) -> None:
        self._installers: dict[str, InstallerClass] = {}

    def register[T: InstallerClass](self, installer: T) -> T:
        if installer.container in self._installers:
            raise ValueError(f"an installer of {installer.container!r} is already registered")
        self._installers[installer.container] = installer
        return installer

    def unregister(self, container: str) -> None:
        del self._installers[container]

    def get(self, container: str) -> InstallerClass:
        try:
            return self._installers[container]
        except KeyError:
            raise KeyError(f"no installer of {container!r}") from None

    def find(self, container: str) -> InstallerClass | None:
        return self._installers.get(container)

    def __iter__(self) -> Iterator[InstallerClass]:
        return iter(self._installers.values())


installers = InstallerRegistry()
