"""Reading client counters from servers: one SSH command per server per cycle."""

import logging
import shlex
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from amnezia_core.awg_dump import parse_dump
from amnezia_core.errors import DumpFormatError
from amnezia_core.xray import XRAY_CONTAINER
from amnezia_core.xray_stats import STATS_COMMAND, parse_stats
from apps.ssh.errors import SshError
from apps.ssh.services import RemoteHost
from apps.traffic.services.recorder import Reading, TrafficRecorder
from apps.traffic.services.targets import PollTarget

logger = logging.getLogger(__name__)

_MARKER = "@@lamnezia-dump"


@dataclass(frozen=True)
class CounterReader:
    """The command that prints the counters of a container, and how to read its output."""

    command: str
    parse: Callable[[str], Sequence[Reading]]


# amnezia-awg2 has `awg`; legacy AmneziaWG and WireGuard containers have only `wg`.
_WG = CounterReader(
    "if command -v awg >/dev/null; then awg show all dump; else wg show all dump; fi", parse_dump
)
# Fails while server.json lacks the statistics blocks: the API is not listening.
_XRAY = CounterReader(STATS_COMMAND, parse_stats)


def reader_for(container: str) -> CounterReader:
    return _XRAY if container == XRAY_CONTAINER else _WG


def dump_script(containers: Iterable[str]) -> str:
    """Print the counters of every container after a marker with the exit code of its command
    and the container's start time, which tells a restart between two readings.
    """
    return "".join(
        f"out=$(docker exec {shlex.quote(name)} sh -c {shlex.quote(reader_for(name).command)}"
        " 2>&1); code=$?\n"
        f"started=$(docker inspect -f '{{{{.State.StartedAt}}}}' {shlex.quote(name)}"
        " 2>/dev/null)\n"
        f'printf "{_MARKER} %s %s %s\\n%s\\n" {shlex.quote(name)} "$code" "$started" "$out"\n'
        for name in containers
    )


@dataclass(frozen=True)
class Dump:
    """What `dump_script` printed for one container; `started` is empty when unknown."""

    code: int
    text: str
    started: str = ""


def split_dumps(output: str) -> dict[str, Dump]:
    """Container name -> its dump, from the output of `dump_script`."""
    heads: dict[str, tuple[int, str, list[str]]] = {}
    lines: list[str] = []
    for line in output.splitlines():
        if line.startswith(f"{_MARKER} "):
            _, name, code, *started = line.split()
            lines = []
            heads[name] = (int(code), "".join(started), lines)
        else:
            lines.append(line)
    return {
        name: Dump(code, "\n".join(text), started) for name, (code, started, text) in heads.items()
    }


class TrafficPoller:
    def __init__(
        self,
        recorder: TrafficRecorder | None = None,
        clock: Callable[[], datetime] = timezone.now,
    ) -> None:
        self._recorder = recorder or TrafficRecorder()
        self._clock = clock

    def poll_all(self, targets: Iterable[PollTarget]) -> None:
        """Poll every server; one that is down is logged and does not stop the others."""
        for target in targets:
            try:
                self.poll(target)
            except SshError as error:
                logger.warning("traffic of %s not collected: %s", target.name, error.code)

    def poll(self, target: PollTarget) -> None:
        with target.connect() as host:
            self.read(host, target.name, target.containers)

    def read(self, host: RemoteHost, server: str, containers: Mapping[str, str]) -> None:
        """Read and store the counters of `containers` (source -> container) of one server.

        A container that gives no reading marks its source unavailable: its traffic is unknown.
        """
        output = host.run(dump_script(containers.values()))
        at = self._clock()
        dumps = split_dumps(output)
        for source, container in containers.items():
            dump = dumps.get(container, Dump(-1, ""))
            if dump.code != 0:
                logger.warning("no counters from %s on %s: exit %s", container, server, dump.code)
                self._recorder.unavailable(source, at)
                continue
            try:
                readings = reader_for(container).parse(dump.text)
            except DumpFormatError as error:
                logger.warning("bad counters from %s on %s: %s", container, server, error)
                self._recorder.unavailable(source, at)
                continue
            self._recorder.record(source, readings, at, dump.started)
