"""The relay container on a server: port checks, image, `docker run` and the agent files.

The container has its own network namespace and publishes the UDP port of every group it
forwards (docs/research/relay-fallback.md): on a host whose Docker sets FORWARD to DROP only a
published port lets clients in. The panel puts the agent and its config into the container; the
agent works on without the panel.
"""

import re
import shlex
from collections.abc import Iterable
from pathlib import Path

from django.utils.translation import gettext as _

from apps.operations.services.context import OperationContext
from apps.protocols.services.container import ProtocolContainer, Transport, log_arguments
from apps.relays.errors import RelayError, RelayErrorCode
from apps.ssh.errors import RemoteFileNotFoundError
from apps.ssh.services import RemoteHost

NAME = "lamnezia-relay"
FOLDER = "/opt/amnezia/relay"
# The panel's web server: HTTP, HTTPS and HTTP/3 (UDP 443) on the panel machine.
RESERVED_PORTS = frozenset({80, 443})

_HERE = Path(__file__).resolve().parent.parent
_PUBLISHED_UDP = re.compile(r":(\d+)->\d+/udp")


def dockerfile() -> str:
    return (_HERE / "container" / "Dockerfile").read_text(encoding="utf-8")


def agent_source() -> bytes:
    return (_HERE / "agent" / "relay_agent.py").read_bytes()


class RelayContainer:
    """The relay container of one server; calls that change it run under the server lock."""

    def __init__(self, host: RemoteHost, ctx: OperationContext) -> None:
        self._host = host
        self._ctx = ctx
        self._docker = ProtocolContainer(host, ctx, NAME)

    @property
    def exists(self) -> bool:
        return bool(self._docker.state())

    def published_ports(self) -> set[int]:
        """UDP ports the container publishes now; empty when there is no container."""
        if not self.exists:
            return set()
        ports = self._docker.inspect("{{json .HostConfig.PortBindings}}")
        return {int(port) for port in re.findall(r'"(\d+)/udp"', ports)}

    def check_ports(self, ports: Iterable[int]) -> None:
        """Refuse a port of the panel's web server or one another program or container uses.

        Ports the relay container publishes already are its own and pass.
        """
        mine = self.published_ports()
        wanted = sorted(set(ports) - mine)
        for port in wanted:
            if port in RESERVED_PORTS:
                raise RelayError(RelayErrorCode.PORT_RESERVED, f"udp {port}")
        published = self._published_by_others()
        for port in wanted:
            if port in published:
                self._ctx.log(published[port])
                raise RelayError(RelayErrorCode.PORT_BUSY, f"udp {port}")
            self._docker.check_port(port, Transport.UDP)

    def install(self, ports: Iterable[int]) -> None:
        """Build the image and start the container; an old container goes first."""
        with self._ctx.step(_("Build the relay image")):
            self._docker.build(dockerfile())
        with self._ctx.step(_("Start the relay container")):
            self._start(ports)

    def apply(self, ports: set[int], config: bytes) -> None:
        """Publish exactly `ports`, then give the agent its config and its current code.

        The ports are checked before the container is started again with the new set.
        """
        if not self.exists:
            raise RelayError(RelayErrorCode.NOT_INSTALLED, NAME)
        if self.published_ports() != ports:
            with self._ctx.step(_("Publish the ports of the groups")):
                self._start(ports)
        with self._ctx.step(_("Write the relay configuration")):
            shell = self._docker.shell
            shell.write_file_atomic(f"{FOLDER}/config.json", config, mode=0o600)
            self._ctx.log(_("Groups forwarded: %(count)d.") % {"count": len(ports)})
        with self._ctx.step(_("Update the relay agent")):
            self._put_agent()

    def remove(self) -> None:
        """Drop the container and its image; the agent and its state go with them."""
        with self._ctx.step(_("Remove the relay container")):
            self._docker.remove()
            self._host.sh(shlex.join(["docker", "rmi", "-f", NAME]))
            if self.exists:
                raise RelayError(RelayErrorCode.NOT_REMOVED, NAME)

    def _start(self, ports: Iterable[int]) -> None:
        self._docker.remove()
        self._docker.run_container(self._run_arguments(ports))
        self._ctx.log(_("The relay container is running."))

    @staticmethod
    def _run_arguments(ports: Iterable[int]) -> list[str]:
        published = [
            argument for port in sorted(ports) for argument in ("-p", f"{port}:{port}/udp")
        ]
        return [
            "docker", "run", "-d", "--name", NAME, "--restart", "always",
            "--cap-add=NET_ADMIN", "--device", "/dev/net/tun",
            "--sysctl", "net.ipv4.ip_forward=1",
            *log_arguments(logs=True), *published, NAME,
        ]  # fmt: skip

    def _put_agent(self) -> None:
        """Write the agent if it changed; a running one is restarted to load the new code."""
        shell, path, source = self._docker.shell, f"{FOLDER}/agent.py", agent_source()
        try:
            current: bytes | None = shell.read_file(path)
        except RemoteFileNotFoundError:
            current = None
        if current == source:
            self._ctx.log(_("The agent is up to date."))
            return
        shell.write_file_atomic(path, source, mode=0o600)
        if current is not None:
            self._docker.restart()
        self._ctx.log(_("The agent is updated."))

    def _published_by_others(self) -> dict[int, str]:
        """UDP ports other containers publish, with the line of `docker ps` naming them.

        lsof does not see a port Docker publishes without its userland proxy.
        """
        lines = self._host.docker("ps", "--format", "{{.Names}} {{.Ports}}").splitlines()
        found: dict[int, str] = {}
        for line in lines:
            name, _space, ports = line.partition(" ")
            if name == NAME:
                continue
            for port in _PUBLISHED_UDP.findall(ports):
                found.setdefault(int(port), line)
        return found
