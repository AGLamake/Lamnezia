"""The reachability probe: a STUN responder on the server that clients' browsers ask over UDP.

AmneziaWG answers nothing to a stranger, so a browser cannot probe the VPN port itself. The
responder runs in its own small container on a separate UDP port and keeps no logs.
"""

import os
import shlex
import socket
from contextlib import ExitStack
from enum import StrEnum
from pathlib import Path

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.accounts.models import User
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.servers.models import PROBE_CONTAINER, Server
from apps.servers.probe import stun_responder
from apps.ssh.errors import CatalogError, ErrorDescription
from apps.ssh.services import RemoteHost

INSTALL_KIND = "servers.probe.install"
REMOVE_KIND = "servers.probe.remove"
DEFAULT_PORT = 3478
CONTAINER = PROBE_CONTAINER
IMAGE = "python:3.13-alpine"
PROBE_DIR = "/opt/lamnezia/probe"
_SCRIPT = Path(stun_responder.__file__)
_SCRIPT_PATH = f"{PROBE_DIR}/{_SCRIPT.name}"
_PULL_TIMEOUT = 300
_PORT_IN_USE_MARKERS = ("address already in use", "port is already allocated")


class ProbeErrorCode(StrEnum):
    PORT_BUSY = "probe_port_busy"
    START_FAILED = "probe_start_failed"
    NO_ANSWER = "probe_no_answer"


PROBE_ERRORS: dict[ProbeErrorCode, ErrorDescription] = {
    ProbeErrorCode.PORT_BUSY: ErrorDescription(
        gettext_lazy("The UDP port is already used on the server."),
        gettext_lazy("Choose another port for the probe."),
    ),
    ProbeErrorCode.START_FAILED: ErrorDescription(
        gettext_lazy("The probe container did not start."),
        gettext_lazy("See the step log. Check that Docker works and the server has internet."),
    ),
    ProbeErrorCode.NO_ANSWER: ErrorDescription(
        gettext_lazy("The probe does not answer from the internet."),
        gettext_lazy(
            "Open the UDP port in the firewall of the server and of the hosting provider, "
            "then install the probe again."
        ),
    ),
}


class ProbeError(CatalogError[ProbeErrorCode]):
    """Installing or checking the probe failed."""

    catalog = PROBE_ERRORS


class StunPing:
    """Asks a STUN responder over UDP the way a browser does; True once it answers."""

    def __init__(self, attempts: int = 3, timeout: float = 2.0) -> None:
        self._attempts = attempts
        self._timeout = timeout

    def __call__(self, host: str, port: int) -> bool:
        family, _type, _proto, _name, address = socket.getaddrinfo(
            host, port, type=socket.SOCK_DGRAM
        )[0]
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self._timeout)
            for _attempt in range(self._attempts):
                transaction_id = os.urandom(12)
                sock.sendto(stun_responder.binding_request(transaction_id), address)
                if self._answered(sock, transaction_id):
                    return True
        return False

    @staticmethod
    def _answered(sock: socket.socket, transaction_id: bytes) -> bool:
        try:
            response = sock.recv(2048)
        except OSError:
            return False
        return stun_responder.mapped_address(response, transaction_id) is not None


class ProbeSetup:
    """Installs and removes the probe container under the server lock (DESIGN.md 5.4).

    The container is unprivileged, read-only and without logs; it only binds its UDP port.
    """

    def __init__(
        self, server: Server, host: RemoteHost, ctx: OperationContext, ping: StunPing
    ) -> None:
        self._server = server
        self._host = host
        self._ctx = ctx
        self._ping = ping

    def install(self, port: int) -> None:
        # Clients stop probing the old port while the container is replaced.
        self._server.probe_port = None
        self._server.save(update_fields=["probe_port"])
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Start the probe container")):
                self._host.write_file_atomic(_SCRIPT_PATH, _SCRIPT.read_bytes(), mode=0o644)
                self._pull_image()
                self._remove_container()
                self._run_container(port)
        with self._ctx.step(_("Check the probe from the internet")):
            if not self._ping(self._server.host, port):
                self._remove_container()
                raise ProbeError(ProbeErrorCode.NO_ANSWER, f"udp {self._server.host}:{port}")
            self._ctx.log(_("The probe answers on UDP port %(port)d.") % {"port": port})
        self._server.probe_port = port
        self._server.save(update_fields=["probe_port"])

    def remove(self) -> None:
        self._server.probe_port = None
        self._server.save(update_fields=["probe_port"])
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Remove the probe container")):
                self._remove_container()
                self._host.run(f"rm -rf -- {shlex.quote(PROBE_DIR)}")

    def _pull_image(self) -> None:
        result = self._host.sh(shlex.join(["docker", "pull", "-q", IMAGE]), timeout=_PULL_TIMEOUT)
        if not result.ok:
            self._ctx.log(result.errors.strip())
            raise ProbeError(ProbeErrorCode.START_FAILED, f"pull: exit {result.exit_code}")

    def _remove_container(self) -> None:
        self._host.sh(shlex.join(["docker", "rm", "-f", CONTAINER]))

    def _run_container(self, port: int) -> None:
        result = self._host.sh(shlex.join(_run_arguments(port)))
        if result.ok:
            return
        self._ctx.log(result.errors.strip())
        self._remove_container()
        busy = any(marker in result.errors for marker in _PORT_IN_USE_MARKERS)
        code = ProbeErrorCode.PORT_BUSY if busy else ProbeErrorCode.START_FAILED
        raise ProbeError(code, f"exit {result.exit_code}")


def _run_arguments(port: int) -> list[str]:
    return [
        "docker", "run", "-d",
        "--name", CONTAINER,
        "--restart", "unless-stopped",
        "--log-driver", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "65534:65534",
        "--memory", "32m",
        "--pids-limit", "16",
        "-p", f"{port}:{port}/udp",
        "-v", f"{PROBE_DIR}:/probe:ro",
        IMAGE,
        "python", "-B", f"/probe/{_SCRIPT.name}", str(port),
    ]  # fmt: skip


def start_install(server: Server, port: int, user: User | None) -> Operation:
    params = {"server_id": server.pk, "port": port}
    return launch(INSTALL_KIND, params, user=user, subject=server.name)


def start_remove(server: Server, user: User | None) -> Operation:
    return launch(REMOVE_KIND, {"server_id": server.pk}, user=user, subject=server.name)
