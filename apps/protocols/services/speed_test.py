"""The port speed test: a temporary server on several ports of a server, iperf3 or WebTransport.

Some providers block or throttle UDP above 9999 (services/ports.py). The administrator starts a
test on a low and a high port and compares the speed from a device in the network of that
provider: in the browser itself over WebTransport (HTTP/3, that is UDP to the very port;
static/js/speed-test.js), or with the iperf3 client where the browser has no WebTransport. The
container keeps no logs and removes itself after the chosen time; the panel can remove it earlier.
"""

import io
import ipaddress
import re
import secrets
import shlex
import tarfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Self

from django.db import models
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.host_setup import log_tail
from apps.protocols.services.installation import used_ports
from apps.protocols.services.ports import LOW_PORTS, free_ports
from apps.servers.models import Server
from apps.ssh.services import RemoteHost

START_KIND = "protocols.speedtest.start"
STOP_KIND = "protocols.speedtest.stop"
# One container for either kind of test, so a new test or "Stop now" removes any of them.
CONTAINER = "lamnezia-iperf"
CERTIFICATE_HASH = "certificate_hash"
MAX_PORTS = 4
DEFAULT_MINUTES = 30
MAX_MINUTES = 180
# The high port to compare with: the one of the Amnezia app, or the next free one.
_HIGH_PORTS = range(55424, 65536)
_BUILD_TIMEOUT = 600
_PORT_IN_USE_MARKERS = ("address already in use", "port is already allocated")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class SpeedTestMode(models.TextChoices):
    BROWSER = "browser", gettext_lazy("In the browser")
    IPERF3 = "iperf3", gettext_lazy("iperf3 on a device")


class TestServer:
    """What listens in the container: its image and the transports of its ports."""

    image: str
    transports: tuple[str, ...]
    memory = "64m"

    def context(self) -> bytes:
        """What `docker build -` reads: a Dockerfile or a tar of the build context."""
        raise NotImplementedError

    def command(self, plan: "SpeedTestPlan") -> list[str]:
        raise NotImplementedError


class Iperf3Server(TestServer):
    """iperf3 takes the control connection over TCP and the UDP test on the same port."""

    image = "lamnezia-iperf3"
    transports = ("tcp", "udp")
    dockerfile = "FROM alpine:3.20\nRUN apk add --no-cache iperf3\n"
    # Every port gets its own iperf3 server; the container ends with `sleep`.
    serve = 'seconds=$1; shift; for port in "$@"; do iperf3 -s -p "$port" & done; sleep "$seconds"'

    def context(self) -> bytes:
        return self.dockerfile.encode()

    def command(self, plan: "SpeedTestPlan") -> list[str]:
        return ["sh", "-c", self.serve, "sh", str(plan.seconds), *map(str, plan.ports)]


class WebTransportServer(TestServer):
    """apps/protocols/images/webtransport: HTTP/3 over UDP, a session only on the plan's token.

    The server makes its certificate itself and writes the hash the browser trusts it by to
    `hash_file`; the private key never leaves the container.
    """

    image = "lamnezia-webtransport"
    transports = ("udp",)
    memory = "256m"
    hash_file = "/tmp/certificate.sha256"
    directory = Path(__file__).resolve().parent.parent / "images" / "webtransport"
    files = ("Dockerfile", "server.py")

    def context(self) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for name in self.files:
                tar.add(self.directory / name, arcname=name)
        return buffer.getvalue()

    def command(self, plan: "SpeedTestPlan") -> list[str]:
        return [str(plan.seconds), plan.token, *map(str, plan.ports)]


SERVERS: dict[str, TestServer] = {
    SpeedTestMode.BROWSER: WebTransportServer(),
    SpeedTestMode.IPERF3: Iperf3Server(),
}


@dataclass(frozen=True)
class SpeedTestPlan:
    """The ports of a speed test, how long it runs and how the administrator measures it.

    `token` is the secret path of a browser test: without it the server accepts no session.
    """

    ports: tuple[int, ...]
    minutes: int = DEFAULT_MINUTES
    mode: str = SpeedTestMode.IPERF3
    token: str = field(default="", repr=False)

    @classmethod
    def new(cls, ports: tuple[int, ...], minutes: int, mode: str) -> Self:
        token = secrets.token_urlsafe(16) if mode == SpeedTestMode.BROWSER else ""
        return cls(ports, minutes, mode, token)

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> Self:
        return cls(
            tuple(int(port) for port in params["ports"]),
            int(params["minutes"]),
            params.get("mode", SpeedTestMode.IPERF3),
            params.get("token", ""),
        )

    def to_params(self) -> dict[str, Any]:
        params = {"ports": list(self.ports), "minutes": self.minutes, "mode": self.mode}
        return {**params, "token": self.token} if self.token else params

    @property
    def server(self) -> TestServer:
        return SERVERS[self.mode]

    @property
    def in_browser(self) -> bool:
        return self.mode == SpeedTestMode.BROWSER

    @property
    def seconds(self) -> int:
        return self.minutes * 60

    def run_arguments(self) -> list[str]:
        """Unprivileged, read-only, without logs; only /tmp is writable."""
        server = self.server
        published = [
            argument
            for port in self.ports
            for transport in server.transports
            for argument in ("-p", f"{port}:{port}/{transport}")
        ]
        return [
            "docker", "run", "-d", "--rm",
            "--name", CONTAINER,
            "--log-driver", "none",
            "--read-only",
            "--tmpfs", "/tmp",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", "65534:65534",
            "--memory", server.memory,
            "--pids-limit", "32",
            *published,
            server.image,
            *server.command(self),
        ]  # fmt: skip


class SpeedTest:
    """Starts and removes the test container under the server lock (DESIGN.md 5.4)."""

    # The browser server needs a moment to start Python and make its certificate.
    _WAIT_FOR_HASH = (
        "for i in $(seq 30); do docker exec {container} cat {file} 2>/dev/null && exit 0; "
        "sleep 0.5; done; exit 1"
    )

    def __init__(self, host: RemoteHost, ctx: OperationContext) -> None:
        self._host = host
        self._ctx = ctx

    def start(self, plan: SpeedTestPlan) -> None:
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Build the speed test image")):
                self._build(plan.server)
            with self._ctx.step(_("Start the speed test")):
                self._remove()
                self._run(plan)
                if plan.in_browser:
                    self._ctx.remember(CERTIFICATE_HASH, self._certificate_hash())
                self._ctx.log(self._listening(plan))

    def stop(self) -> None:
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Remove the speed test container")):
                self._remove()

    def _build(self, server: TestServer) -> None:
        build = shlex.join(["docker", "build", "-q", "-t", server.image, "-"])
        result = self._host.sh(build, stdin=server.context(), timeout=_BUILD_TIMEOUT)
        if not result.ok:
            log_tail(self._ctx, result)
            raise SetupError(SetupErrorCode.SPEED_TEST_FAILED, f"build: exit {result.exit_code}")

    def _run(self, plan: SpeedTestPlan) -> None:
        result = self._host.sh(shlex.join(plan.run_arguments()))
        if result.ok:
            return
        log_tail(self._ctx, result)
        self._remove()
        busy = any(marker in result.errors for marker in _PORT_IN_USE_MARKERS)
        code = SetupErrorCode.SPEED_TEST_PORT_BUSY if busy else SetupErrorCode.SPEED_TEST_FAILED
        raise SetupError(code, f"run: exit {result.exit_code}")

    def _certificate_hash(self) -> str:
        """The hash the server wrote once it listens; a server that died writes none."""
        script = self._WAIT_FOR_HASH.format(
            container=CONTAINER, file=shlex.quote(WebTransportServer.hash_file)
        )
        result = self._host.sh(script)
        value = result.output.strip()
        if result.ok and _HASH.match(value):
            return value
        self._remove()
        raise SetupError(SetupErrorCode.SPEED_TEST_FAILED, "no certificate hash")

    @staticmethod
    def _listening(plan: SpeedTestPlan) -> str:
        if plan.in_browser:
            line = _("WebTransport listens on UDP ports %(ports)s for %(minutes)d minutes.")
        else:
            line = _("iperf3 listens on ports %(ports)s (TCP and UDP) for %(minutes)d minutes.")
        return line % {"ports": ", ".join(map(str, plan.ports)), "minutes": plan.minutes}

    def _remove(self) -> None:
        self._host.sh(shlex.join(["docker", "rm", "-f", CONTAINER]))


@dataclass(frozen=True)
class PortTarget:
    """A port of a running browser test and the address the page measures it at."""

    port: int
    url: str

    @property
    def origin(self) -> str:
        """What the page's CSP must allow: the URL without the secret path."""
        return self.url.rsplit("/", 1)[0]


@dataclass(frozen=True)
class RunningTest:
    """A speed test the panel started that has not ended yet, as the page shows it.

    `started_by` is the id of the user who started it: a portal user sees only their own test.
    """

    plan: SpeedTestPlan
    until: datetime
    host: str
    certificate_hash: str = ""
    started_by: int | None = None

    @classmethod
    def of(cls, server: Server, now: datetime) -> Self | None:
        """From the latest start or removal of the server: a failed start left nothing."""
        latest = (
            Operation.objects.filter(kind__in=(START_KIND, STOP_KIND), params__server_id=server.pk)
            .order_by("-created_at")
            .first()
        )
        if not latest or latest.kind != START_KIND or latest.status != OperationStatus.SUCCEEDED:
            return None
        plan = SpeedTestPlan.from_params(latest.params)
        until = (latest.finished_at or latest.created_at) + timedelta(minutes=plan.minutes)
        if until <= now:
            return None
        certificate_hash = latest.params.get(CERTIFICATE_HASH, "")
        return cls(plan, until, server.host, certificate_hash, latest.created_by_id)

    @property
    def targets(self) -> list[PortTarget]:
        """The WebTransport addresses of a browser test; none for iperf3."""
        if not (self.plan.in_browser and self.certificate_hash):
            return []
        return [
            PortTarget(port, f"https://{self.host}:{port}/{self.plan.token}")
            for port in self.plan.ports
        ]


def browser_reachable(host: str) -> bool:
    """A page can measure a domain or an IPv4 address: CSP has no syntax for IPv6 ones."""
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)
    except ValueError:
        return True


def default_ports(server: Server) -> tuple[int, ...]:
    """A free port up to 9999 and a high one, free for both kinds of test, to compare."""
    taken = used_ports(server)
    transports = Iperf3Server.transports
    return (
        *free_ports(LOW_PORTS, taken, transports),
        *free_ports(_HIGH_PORTS, taken, transports),
    )


def start(server: Server, plan: SpeedTestPlan, user: User, *, ip: str | None = None) -> Operation:
    params = {"server_id": server.pk, **plan.to_params()}
    operation = launch(START_KIND, params, user=user, subject=server.name)
    # The token stays out of the journal: it lets anyone into the test.
    data = {"ports": list(plan.ports), "minutes": plan.minutes, "mode": plan.mode}
    data["operation"] = operation.pk
    audit.log(START_KIND, actor=user, target=server, data=data, ip=ip)
    return operation


def stop(server: Server, user: User, *, ip: str | None = None) -> Operation:
    operation = launch(STOP_KIND, {"server_id": server.pk}, user=user, subject=server.name)
    audit.log(STOP_KIND, actor=user, target=server, data={"operation": operation.pk}, ip=ip)
    return operation
