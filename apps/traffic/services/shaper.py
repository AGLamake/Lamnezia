"""Writing the speed limits of an instance into its container and keeping them after a restart."""

from collections.abc import Callable, Iterable
from contextlib import ExitStack

from django.utils.translation import gettext_lazy as _

from amnezia_core.containers import WgContainer
from amnezia_core.start_script import (
    START_SCRIPT_MODE,
    START_SCRIPT_PATH,
    XRAY_STOP_LINE,
    with_line_after,
    with_startup_line,
)
from amnezia_core.xray import XRAY_CONTAINER
from amnezia_core.xray_stats import STATS_COMMAND
from apps.keys.errors import PeerError, PeerErrorCode
from apps.keys.services.peers import StepRecorder
from apps.keys.services.xray_clients import XrayClientManager
from apps.ssh.errors import RemoteFileNotFoundError
from apps.ssh.services import DockerContainer, RemoteHost
from apps.traffic.services.shaping import (
    SPEED_SCRIPT_MODE,
    SpeedRule,
    speed_script,
    speed_script_path,
)
from apps.traffic.services.xray_shaping import (
    SPEED_NFT_MODE,
    SPEED_NFT_PATH,
    XraySpeedRule,
    speed_marks,
    speed_nft,
)

# Amnezia images ship the busybox tc, which cannot add HTB classes; the full one comes from
# Alpine's iproute2 (split into iproute2-tc since Alpine 3.13).
_INSTALL_TC = (
    "tc -V 2>/dev/null | grep -q iproute2 && exit 0\n"
    "apk add --no-cache iproute2-tc >/dev/null 2>&1 || apk add --no-cache iproute2 >/dev/null\n"
    "tc -V\n"
)
# The XRay image has no firewall tools; the package stays until the container is re-created.
_INSTALL_NFT = "command -v nft >/dev/null || apk add --no-cache nftables >/dev/null\nnft -v\n"
# The API comes up with xray: a few seconds for it after a restart.
_STATS_ANSWER = (
    f"for i in 1 2 3 4 5; do {STATS_COMMAND} >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1"
)


class StartScript:
    """start.sh of a container, which runs a line of the panel on every start."""

    def __init__(self, container: DockerContainer, steps: StepRecorder) -> None:
        self._container = container
        self._steps = steps

    def add(self, line: str, insert: Callable[[str, str], str | None]) -> None:
        """Put `line` where `insert` places it; a script without such a place is kept."""
        try:
            current = self._container.read_file(START_SCRIPT_PATH).decode()
        except RemoteFileNotFoundError:
            current = ""
        updated = insert(current, line)
        if updated is None:
            self._steps.log(f"{START_SCRIPT_PATH}: no place to start the limits from")
        elif updated != current:
            self._container.write_file_atomic(
                START_SCRIPT_PATH, updated.encode(), START_SCRIPT_MODE
            )


class SpeedShaper:
    """Replaces the tc rules of a container's interface, under the server lock (DESIGN.md 5.4).

    The rules are kept in a script next to the protocol config, and start.sh runs it after the
    interface comes up, so the limits outlive a container restart. Safe to run again.
    """

    def __init__(self, host: RemoteHost, steps: StepRecorder, kind: WgContainer) -> None:
        self._host = host
        self._kind = kind
        self._container: DockerContainer = host.container(kind.name)
        self._steps = steps

    def apply(self, rules: Iterable[SpeedRule]) -> None:
        rules = list(rules)
        path = speed_script_path(self._kind)
        with ExitStack() as held:
            with self._steps.step(_("Lock the server")):
                held.enter_context(self._host.lock())
            with self._steps.step(_("Install tc in the container")):
                self._steps.log(self._container.run(_INSTALL_TC).strip())
            with self._steps.step(_("Apply the speed limits")):
                script = speed_script(self._kind.interface, rules)
                self._container.write_file_atomic(path, script.encode(), SPEED_SCRIPT_MODE)
                self._container.run(f"sh {path}")
                self._steps.log(f"limited keys on {self._kind.interface}: {len(rules)}")
            with self._steps.step(_("Keep the limits after a restart")):
                line = f"[ -f {path} ] && sh {path} || true"
                StartScript(self._container, self._steps).add(line, with_startup_line)


class XraySpeedShaper:
    """Limits XRay keys: nftables rules by socket mark, and server.json routes that set it.

    The nft file lives in the data volume and start.sh loads it before xray starts, so the
    limits outlive a restart. server.json changes, and xray restarts, only when the set of
    limited keys does; a new speed reloads the nft rules alone. Running again also puts back
    what the app dropped when it re-created the container or rebuilt server.json.
    """

    def __init__(self, host: RemoteHost, steps: StepRecorder, source: str) -> None:
        self._host = host
        self._container: DockerContainer = host.container(XRAY_CONTAINER)
        self._steps = steps
        self._source = source

    def apply(self, rules: Iterable[XraySpeedRule]) -> None:
        marks = speed_marks(rules)
        with ExitStack() as held:
            with self._steps.step(_("Lock the server")):
                held.enter_context(self._host.lock())
            with self._steps.step(_("Install nftables in the container")):
                self._steps.log(self._container.run(_INSTALL_NFT).strip())
            with self._steps.step(_("Apply the speed limits")):
                nft = speed_nft(marks)
                self._container.write_file_atomic(SPEED_NFT_PATH, nft.encode(), SPEED_NFT_MODE)
                self._container.run(f"nft -f {SPEED_NFT_PATH}")
                self._steps.log(f"limited XRay keys: {len(marks)}")
            with self._steps.step(_("Keep the limits after a restart")):
                line = f"[ -f {SPEED_NFT_PATH} ] && nft -f {SPEED_NFT_PATH} || true"
                StartScript(self._container, self._steps).add(
                    line, lambda script, text: with_line_after(script, text, XRAY_STOP_LINE)
                )
        routes = {rule.email: mark for rule, mark in marks.items()}
        XrayClientManager(self._host, self._steps, self._source).update_config(
            lambda config: config.set_speed_marks(routes)
        )
        with self._steps.step(_("Check that xray serves the limits")):
            if not self._container.sh(_STATS_ANSWER).ok:
                raise PeerError(PeerErrorCode.NOT_SERVING, "statistics API does not answer")
