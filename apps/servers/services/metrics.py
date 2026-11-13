"""Host metrics from one shell script: CPU counters, memory, disk, uptime and Amnezia containers."""

from dataclasses import dataclass

from apps.servers.models import PROBE_CONTAINER

_MARKER = "@@lamnezia-"
_CONTAINER_PREFIX = "amnezia"
# cpu user nice system idle iowait irq softirq steal guest guest_nice; guest time is already
# counted in user, so only the first eight fields add up to the total.
_CPU_FIELDS = 8

# One SSH command per reading. CPU load comes from the difference between two readings, so the
# script does not sleep to measure it. Docker may be missing: then the container list is empty.
METRICS_SCRIPT = (
    f"echo {_MARKER}stat; head -n 1 /proc/stat\n"
    f"echo {_MARKER}meminfo; cat /proc/meminfo\n"
    f"echo {_MARKER}uptime; cat /proc/uptime\n"
    f"echo {_MARKER}disk; df -Pk / | awk 'NR == 2 {{print $2, $4}}'\n"
    f"echo {_MARKER}containers; docker ps -a --format '{{{{.Names}}}} {{{{.State}}}}' 2>/dev/null\n"
    "true\n"
)


@dataclass(frozen=True)
class CpuTimes:
    """Cumulative CPU time since boot from /proc/stat, in clock ticks."""

    total: int
    idle: int

    def busy_percent_since(self, earlier: "CpuTimes") -> float | None:
        """CPU load between two readings; None after a reboot, when the counters start again."""
        total = self.total - earlier.total
        idle = self.idle - earlier.idle
        if total <= 0 or idle < 0:
            return None
        return round(100 * (total - idle) / total, 1)


@dataclass(frozen=True)
class ContainerState:
    name: str
    state: str

    @property
    def running(self) -> bool:
        return self.state == "running"


@dataclass(frozen=True)
class HostMetrics:
    """One reading of a server; a value the server did not report is None."""

    cpu: CpuTimes | None = None
    memory_total: int | None = None
    memory_available: int | None = None
    disk_total: int | None = None
    disk_free: int | None = None
    uptime: int | None = None
    containers: tuple[ContainerState, ...] = ()


def parse_metrics(output: str) -> HostMetrics:
    sections = _split_sections(output)
    memory = _parse_meminfo(sections.get("meminfo", ""))
    disk = _numbers(sections.get("disk", ""), 2)
    return HostMetrics(
        cpu=_parse_stat(sections.get("stat", "")),
        memory_total=memory.get("MemTotal"),
        memory_available=memory.get("MemAvailable"),
        disk_total=disk[0] * 1024 if disk else None,
        disk_free=disk[1] * 1024 if disk else None,
        uptime=_parse_uptime(sections.get("uptime", "")),
        containers=_parse_containers(sections.get("containers", "")),
    )


def _split_sections(output: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    lines: list[str] = []
    for line in output.splitlines():
        if line.startswith(_MARKER):
            lines = sections.setdefault(line.removeprefix(_MARKER).strip(), [])
        else:
            lines.append(line)
    return {name: "\n".join(text) for name, text in sections.items()}


def _parse_stat(text: str) -> CpuTimes | None:
    fields = text.split()
    if not fields or fields[0] != "cpu":
        return None
    ticks = _numbers(" ".join(fields[1 : _CPU_FIELDS + 1]), _CPU_FIELDS)
    if not ticks:
        return None
    return CpuTimes(total=sum(ticks), idle=ticks[3] + ticks[4])


def _parse_meminfo(text: str) -> dict[str, int]:
    """Field name -> bytes; /proc/meminfo reports kilobytes."""
    memory: dict[str, int] = {}
    for line in text.splitlines():
        name, _, value = line.partition(":")
        amount = value.split()[:1]
        if amount and amount[0].isdigit():
            memory[name] = int(amount[0]) * 1024
    return memory


def _parse_uptime(text: str) -> int | None:
    try:
        return int(float(text.split(maxsplit=1)[0]))
    except (IndexError, ValueError):
        return None


def _parse_containers(text: str) -> tuple[ContainerState, ...]:
    containers = []
    for line in text.splitlines():
        name, _, state = line.strip().partition(" ")
        if (name.startswith(_CONTAINER_PREFIX) or name == PROBE_CONTAINER) and state:
            containers.append(ContainerState(name, state))
    return tuple(containers)


def _numbers(text: str, count: int) -> list[int] | None:
    """Exactly `count` non-negative integers separated by spaces, or None."""
    fields = text.split()
    if len(fields) != count or not all(field.isdigit() for field in fields):
        return None
    return [int(field) for field in fields]
