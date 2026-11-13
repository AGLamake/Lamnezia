"""Shell script and systemd units that keep TSO/GSO off in the WireGuard-family containers.

On some VPS the outgoing interface of the container glues TCP segments together (TSO/GSO) and
the glued packets are lost on the way, so upload through the VPN drops several times. Docker
recreates the interface with every container restart, so a systemd timer on the host turns the
offloads off again. The interface is the one of the default route in the network namespace of
the container; nothing inside the container, its config or its keys changes.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Self

from amnezia_core.containers import WG_CONTAINERS

UNIT = "lamnezia-offload"
OFFLOAD_DIR = "/opt/lamnezia/offload"
SCRIPT_PATH = f"{OFFLOAD_DIR}/offload.sh"
SERVICE_PATH = f"/etc/systemd/system/{UNIT}.service"
TIMER_PATH = f"/etc/systemd/system/{UNIT}.timer"
SCRIPT_MODE = 0o755
UNIT_MODE = 0o644

# Commands the script needs on the host and the Debian packages that provide them.
PACKAGES = {"ethtool": "ethtool", "ip": "iproute2", "nsenter": "util-linux"}
MISSING_COMMANDS = (
    f"for cmd in {' '.join(PACKAGES)}; do\n"
    '  command -v "$cmd" >/dev/null 2>&1 || echo "$cmd"\n'
    "done\n"
)
HAS_SYSTEMD = "[ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1"

# Value of an offload the script could not read: no ethtool or an unknown interface.
UNKNOWN = "-"
_TIMER_LINE = "timer "

_FUNCTIONS = r"""
lamnezia_offload_feature() {
  value=$(nsenter -t "$1" -n ethtool -k "$2" 2>/dev/null \
    | awk -v f="$3:" '$1 == f { print $2; exit }')
  echo "${value:--}"
}

lamnezia_offload() {
  mode=$1
  for name in __CONTAINERS__; do
    pid=$(docker inspect -f '{{.State.Pid}}' "$name" 2>/dev/null) || continue
    [ "${pid:-0}" -gt 0 ] 2>/dev/null || continue
    dev=$(nsenter -t "$pid" -n ip -4 route show default 2>/dev/null \
      | awk '{ for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit } }')
    [ -n "$dev" ] || continue
    tso=$(lamnezia_offload_feature "$pid" "$dev" tcp-segmentation-offload)
    gso=$(lamnezia_offload_feature "$pid" "$dev" generic-segmentation-offload)
    case $mode in
      apply)
        if [ "$tso" = on ] || [ "$gso" = on ]; then
          nsenter -t "$pid" -n ethtool -K "$dev" tso off gso off >/dev/null 2>&1 \
            && echo "$name $dev: TSO/GSO turned off"
        fi
        ;;
      restore)
        nsenter -t "$pid" -n ethtool -K "$dev" tso on gso on >/dev/null 2>&1 || true
        ;;
      status)
        echo "$name $dev $tso $gso"
        ;;
    esac
  done
}
""".replace("__CONTAINERS__", " ".join(WG_CONTAINERS))

SCRIPT = (
    "#!/bin/sh\n"
    "# Lamnezia: TSO/GSO off on the outgoing interface of the AmneziaWG and WireGuard containers.\n"
    "# The panel rewrites this file and removes it with the workaround; edits are lost.\n"
    f"{_FUNCTIONS}\n"
    'lamnezia_offload "${1:-apply}"\n'
)

# The manager logs every run of the service; only the lines of the script about a change pass.
SERVICE = f"""[Unit]
Description=Lamnezia: TSO/GSO off in the AmneziaWG and WireGuard containers
After=docker.service

[Service]
Type=oneshot
ExecStart=/bin/sh {SCRIPT_PATH} apply
SyslogLevel=notice
LogLevelMax=notice
"""

# Docker recreates the interface when the container restarts: the timer catches it in 30 s.
TIMER = f"""[Unit]
Description=Lamnezia: keep TSO/GSO off after the containers restart

[Timer]
OnBootSec=20s
OnUnitActiveSec=30s
AccuracySec=5s
Unit={UNIT}.service

[Install]
WantedBy=timers.target
"""

# What the workaround puts on the host: (path, content, mode).
FILES = (
    (SCRIPT_PATH, SCRIPT, SCRIPT_MODE),
    (SERVICE_PATH, SERVICE, UNIT_MODE),
    (TIMER_PATH, TIMER, UNIT_MODE),
)

ENABLE = (
    "set -e\n"
    "systemctl daemon-reload\n"
    f"systemctl enable --now {UNIT}.timer >/dev/null 2>&1\n"
    f"sh {SCRIPT_PATH} apply\n"
)

# The offloads are turned back on, as Docker sets them for a new interface.
DISABLE = (
    f"systemctl disable --now {UNIT}.timer >/dev/null 2>&1 || true\n"
    f"systemctl stop {UNIT}.service >/dev/null 2>&1 || true\n"
    f"rm -f {SERVICE_PATH} {TIMER_PATH}\n"
    "systemctl daemon-reload\n"
    f"systemctl reset-failed {UNIT}.service >/dev/null 2>&1 || true\n"
    f"{_FUNCTIONS}\n"
    "lamnezia_offload restore\n"
    f"rm -rf -- {OFFLOAD_DIR}\n"
)

STATUS = (
    f'echo "{_TIMER_LINE}$(systemctl is-active {UNIT}.timer 2>/dev/null || true)"\n'
    f"{_FUNCTIONS}\n"
    "lamnezia_offload status\n"
)

_STATUS_LINE = re.compile(
    r"^(?P<container>[\w.-]+) (?P<interface>[\w.@-]+) (?P<tso>on|off|-) (?P<gso>on|off|-)$"
)


@dataclass(frozen=True)
class InterfaceOffload:
    """TSO and GSO of the outgoing interface of one container: `on`, `off` or `UNKNOWN`."""

    container: str
    interface: str
    tso: str
    gso: str

    @property
    def glues(self) -> bool:
        """The interface glues TCP segments, which slows upload down on some VPS."""
        return "on" in (self.tso, self.gso)

    @property
    def known(self) -> bool:
        return UNKNOWN not in (self.tso, self.gso)

    def to_json(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "interface": self.interface,
            "tso": self.tso,
            "gso": self.gso,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        return cls(data["container"], data["interface"], data["tso"], data["gso"])


@dataclass(frozen=True)
class OffloadReading:
    """What STATUS printed: whether the timer runs, and the interfaces of running containers."""

    timer_active: bool
    interfaces: tuple[InterfaceOffload, ...]

    @classmethod
    def parse(cls, text: str) -> Self:
        timer_active = False
        interfaces = []
        for line in map(str.strip, text.splitlines()):
            if line.startswith(_TIMER_LINE):
                timer_active = line.removeprefix(_TIMER_LINE) == "active"
            elif match := _STATUS_LINE.match(line):
                interfaces.append(InterfaceOffload(**match.groupdict()))
        return cls(timer_active, tuple(interfaces))

    @property
    def glued(self) -> list[InterfaceOffload]:
        return [interface for interface in self.interfaces if interface.glues]


def install_list(missing: Iterable[str]) -> list[str]:
    """Packages that provide the missing commands."""
    return [PACKAGES[command] for command in missing if command in PACKAGES]
