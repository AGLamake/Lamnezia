"""Reading client counters from servers: one SSH command per server per cycle."""

import logging
import shlex
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from amnezia_core.awg_dump import PeerStats, parse_dump
from amnezia_core.containers import WG_CONTAINERS
from amnezia_core.errors import DumpFormatError
from amnezia_core.xray import XRAY_CONTAINER
from amnezia_core.xray_stats import STATS_COMMAND, parse_stats
from apps.ssh.errors import SshError
from apps.ssh.services import RemoteHost
from apps.traffic.services.recorder import Reading, TrafficRecorder
from apps.traffic.services.shaping import shaping_probe
from apps.traffic.services.targets import PollTarget, peer_owners
from apps.traffic.services.xray_shaping import xray_shaping_probe

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


@dataclass(frozen=True)
class Channel:
    """A container to read and, when it has several interfaces of different sources, the one."""

    container: str
    interface: str = ""

    @property
    def label(self) -> str:
        """The name of its dump in the output of `dump_script`."""
        return f"{self.container}@{self.interface}" if self.interface else self.container


def dump_script(channels: Iterable[str | Channel]) -> str:
    """Print the counters of every container after a marker with the exit code of its command,
    what is left of its speed limits (`shaping_probe`, `xray_shaping_probe`; `-` for an
    interface of a container with several) and the container's start time, which tells a
    restart between two readings.

    The start time of an interface also has its index, which changes when the interface is
    created again: its peers then count from zero, though the container ran on.
    """
    return "".join(map(_channel_script, channels))


def _channel_script(channel: str | Channel) -> str:
    if isinstance(channel, str):
        channel = Channel(channel)
    name = shlex.quote(channel.container)
    command = shlex.quote(reader_for(channel.container).command)
    started = f"$(docker inspect -f '{{{{.State.StartedAt}}}}' {name} 2>/dev/null)"
    if channel.interface:
        index = shlex.quote(f"/sys/class/net/{channel.interface}/ifindex")
        started += f"/$(docker exec {name} cat {index} 2>/dev/null)"
    shaping = ""
    if probe := _shaping_probe(channel):
        # Only the word it prints, so nothing else can break the marker line.
        shaping = f"$(docker exec {name} sh -c {shlex.quote(probe)} 2>/dev/null | tr -cd a-z_)"
    return (
        f"out=$(docker exec {name} sh -c {command} 2>&1); code=$?\n"
        f'started="{started}"; shaping="{shaping}"\n'
        f'printf "{_MARKER} %s %s %s %s\\n%s\\n" {shlex.quote(channel.label)} "$code"'
        ' "${shaping:--}" "$started" "$out"\n'
    )


def _shaping_probe(channel: Channel) -> str:
    """The probe of the speed limits of the container; none for one of its several interfaces."""
    if channel.interface:
        return ""
    if channel.container == XRAY_CONTAINER:
        return xray_shaping_probe()
    kind = WG_CONTAINERS.get(channel.container)
    return shaping_probe(kind) if kind else ""


@dataclass(frozen=True)
class Dump:
    """What `dump_script` printed for one container; `started` and `shaping` are empty when
    unknown."""

    code: int
    text: str
    started: str = ""
    shaping: str = ""


def split_dumps(output: str) -> dict[str, Dump]:
    """Channel label -> its dump, from the output of `dump_script`."""
    heads: dict[str, tuple[int, str, str, list[str]]] = {}
    lines: list[str] = []
    for line in output.splitlines():
        if line.startswith(f"{_MARKER} "):
            _, name, code, shaping, *started = line.split()
            lines = []
            heads[name] = (int(code), "".join(started), shaping.strip("-"), lines)
        else:
            lines.append(line)
    return {
        name: Dump(code, "\n".join(text), started, shaping)
        for name, (code, started, shaping, text) in heads.items()
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
        """Poll every server; one that is down is logged and does not stop the others.

        Its sources are unavailable: their traffic, and that of the group keys a member holds,
        is unknown until a reading works.
        """
        for target in targets:
            try:
                self.poll(target)
            except SshError as error:
                logger.warning("traffic of %s not collected: %s", target.name, error.code)
                at = self._clock()
                for source in target.containers:
                    self._recorder.unavailable(source, at, target.key_sources.get(source, ""))

    def poll(self, target: PollTarget) -> None:
        with target.connect() as host:
            self.read(host, target.name, target.containers, target.key_sources, target.interfaces)

    def read(
        self,
        host: RemoteHost,
        server: str,
        containers: Mapping[str, str],
        key_sources: Mapping[str, str] | None = None,
        interfaces: Mapping[str, str] | None = None,
    ) -> None:
        """Read and store the counters of `containers` (source -> container) of one server.

        A container that gives no reading marks its source unavailable: its traffic is unknown.
        `key_sources` and `interfaces` are as `PollTarget` has them. An interface that is not
        there gives an empty reading: its peers are gone, not unknown.
        """
        key_sources, interfaces = key_sources or {}, interfaces or {}
        channels = {
            source: Channel(container, interfaces.get(source, ""))
            for source, container in containers.items()
        }
        output = host.run(dump_script(dict.fromkeys(channels.values())))
        at = self._clock()
        dumps = split_dumps(output)
        owners = dict(peer_owners)
        for source, channel in channels.items():
            dump = dumps.get(channel.label, Dump(-1, ""))
            if dump.code != 0:
                logger.warning(
                    "no counters from %s on %s: exit %s", channel.label, server, dump.code
                )
                self._recorder.unavailable(source, at, key_sources.get(source, ""))
                continue
            try:
                readings = _of_interface(reader_for(channel.container).parse(dump.text), channel)
            except DumpFormatError as error:
                logger.warning("bad counters from %s on %s: %s", channel.label, server, error)
                self._recorder.unavailable(source, at, key_sources.get(source, ""))
                continue
            self._recorder.record(
                source, readings, at, dump.started, key_sources.get(source, ""), owners=owners
            )
            if dump.shaping:
                self._recorder.shaping(source, dump.shaping)


def _of_interface(readings: Sequence[Reading], channel: Channel) -> Sequence[Reading]:
    if not channel.interface:
        return readings
    return [
        peer
        for peer in readings
        if isinstance(peer, PeerStats) and peer.interface == channel.interface
    ]
