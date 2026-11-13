"""The port speed test: a temporary iperf3 server on several ports of a server.

Some providers block or throttle UDP above 9999 (services/ports.py). The administrator starts
iperf3 on a low and a high port, runs the iperf3 client on a device in the network of that
provider and compares the speed. The container keeps no logs and removes itself after the
chosen time; the panel can remove it earlier.
"""

import shlex
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Self

from django.utils.translation import gettext as _

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
CONTAINER = "lamnezia-iperf"
IMAGE = "lamnezia-iperf3"
DOCKERFILE = "FROM alpine:3.20\nRUN apk add --no-cache iperf3\n"
# iperf3 takes the control connection over TCP and the UDP test on the same port.
TRANSPORTS = ("tcp", "udp")
MAX_PORTS = 4
DEFAULT_MINUTES = 30
MAX_MINUTES = 180
# The high port to compare with: the one of the Amnezia app, or the next free one.
_HIGH_PORTS = range(55424, 65536)
_BUILD_TIMEOUT = 600
_PORT_IN_USE_MARKERS = ("address already in use", "port is already allocated")
# Every port gets its own iperf3 server; the container ends with `sleep`.
_SERVE = 'seconds=$1; shift; for port in "$@"; do iperf3 -s -p "$port" & done; sleep "$seconds"'


@dataclass(frozen=True)
class SpeedTestPlan:
    """The ports of a speed test and how long it runs."""

    ports: tuple[int, ...]
    minutes: int = DEFAULT_MINUTES

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> Self:
        return cls(tuple(int(port) for port in params["ports"]), int(params["minutes"]))

    def to_params(self) -> dict[str, Any]:
        return {"ports": list(self.ports), "minutes": self.minutes}

    def run_arguments(self) -> list[str]:
        """Unprivileged, read-only, without logs; only /tmp is writable for iperf3."""
        published = [
            argument
            for port in self.ports
            for transport in TRANSPORTS
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
            "--memory", "64m",
            "--pids-limit", "32",
            *published,
            IMAGE,
            "sh", "-c", _SERVE, "sh", str(self.minutes * 60), *map(str, self.ports),
        ]  # fmt: skip


class SpeedTest:
    """Starts and removes the iperf3 container under the server lock (DESIGN.md 5.4)."""

    def __init__(self, host: RemoteHost, ctx: OperationContext) -> None:
        self._host = host
        self._ctx = ctx

    def start(self, plan: SpeedTestPlan) -> None:
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Build the iperf3 image")):
                self._build()
            with self._ctx.step(_("Start iperf3")):
                self._remove()
                self._run(plan)
                self._ctx.log(
                    _("iperf3 listens on ports %(ports)s (TCP and UDP) for %(minutes)d minutes.")
                    % {"ports": ", ".join(map(str, plan.ports)), "minutes": plan.minutes}
                )

    def stop(self) -> None:
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Remove the iperf3 container")):
                self._remove()

    def _build(self) -> None:
        build = shlex.join(["docker", "build", "-q", "-t", IMAGE, "-"])
        result = self._host.sh(build, stdin=DOCKERFILE.encode(), timeout=_BUILD_TIMEOUT)
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

    def _remove(self) -> None:
        self._host.sh(shlex.join(["docker", "rm", "-f", CONTAINER]))


@dataclass(frozen=True)
class RunningTest:
    """A speed test the panel started that has not ended yet, as the page shows it."""

    plan: SpeedTestPlan
    until: datetime

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
        return cls(plan, until) if until > now else None


def default_ports(server: Server) -> tuple[int, ...]:
    """A free port up to 9999 and a high one, to compare."""
    taken = used_ports(server)
    return (
        *free_ports(LOW_PORTS, taken, TRANSPORTS),
        *free_ports(_HIGH_PORTS, taken, TRANSPORTS),
    )


def start(server: Server, plan: SpeedTestPlan, user: User, *, ip: str | None = None) -> Operation:
    params = {"server_id": server.pk, **plan.to_params()}
    operation = launch(START_KIND, params, user=user, subject=server.name)
    data = {**plan.to_params(), "operation": operation.pk}
    audit.log(START_KIND, actor=user, target=server, data=data, ip=ip)
    return operation


def stop(server: Server, user: User, *, ip: str | None = None) -> Operation:
    operation = launch(STOP_KIND, {"server_id": server.pk}, user=user, subject=server.name)
    audit.log(STOP_KIND, actor=user, target=server, data={"operation": operation.pk}, ip=ip)
    return operation
