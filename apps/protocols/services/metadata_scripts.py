"""Shell script and systemd units that close the cloud metadata service to the VPN clients.

A VPN client goes out to the network through the host, so it can reach 169.254.169.254: the
metadata service of a cloud server, which hands out the tokens of the server's service account
on AWS, GCP, Azure and Yandex Cloud. A rule in the DOCKER-USER chain of the host drops traffic
from the Docker bridges of the Amnezia containers (the default `bridge` and `amnezia-dns-net`)
to the whole link-local range 169.254.0.0/16. The containers do not change, and the host itself
still reaches the metadata: its own traffic does not pass the FORWARD chain.

Rules of iptables are lost on reboot and a firewall reload flushes them, so a systemd timer runs
the script again; the script adds only what is missing.
"""

import re
from dataclasses import dataclass
from typing import Self

from apps.protocols.services.host_scripts import AMNEZIA_NETWORK

UNIT = "lamnezia-metadata"
CHAIN = "LAMNEZIA-METADATA"
LINK_LOCAL = "169.254.0.0/16"
METADATA_DIR = "/opt/lamnezia/metadata"
SCRIPT_PATH = f"{METADATA_DIR}/metadata.sh"
SERVICE_PATH = f"/etc/systemd/system/{UNIT}.service"
TIMER_PATH = f"/etc/systemd/system/{UNIT}.timer"
SCRIPT_MODE = 0o755
UNIT_MODE = 0o644

HAS_SYSTEMD = "[ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1"

# The Docker networks of the Amnezia containers; their bridges are the interfaces closed.
NETWORKS = ("bridge", AMNEZIA_NETWORK)

_TIMER_LINE = "timer "
_JUMP_LINE = "jump "
_CLOSED_LINE = "closed "

_FUNCTIONS = (
    r"""
lamnezia_metadata_bridges() {
  for net in __NETWORKS__; do
    out=$(docker network inspect -f \
      '{{.Id}} {{index .Options "com.docker.network.bridge.name"}}' "$net" 2>/dev/null) \
      || continue
    id=${out%% *}
    dev=${out#* }
    case $dev in
      ""|"$out"|"<no value>") dev="br-$(printf '%s' "$id" | cut -c1-12)" ;;
    esac
    echo "$dev"
  done
}

lamnezia_metadata_apply() {
  # Docker makes DOCKER-USER when it starts; until then there is nothing to add to.
  iptables -n -L DOCKER-USER >/dev/null 2>&1 || return 0
  iptables -n -L __CHAIN__ >/dev/null 2>&1 || iptables -N __CHAIN__
  for dev in $(lamnezia_metadata_bridges); do
    iptables -C __CHAIN__ -i "$dev" -d __NET__ -j DROP 2>/dev/null && continue
    iptables -A __CHAIN__ -i "$dev" -d __NET__ -j DROP && echo "$dev: __NET__ closed"
  done
  iptables -C DOCKER-USER -j __CHAIN__ 2>/dev/null \
    || { iptables -I DOCKER-USER -j __CHAIN__ && echo "DOCKER-USER: __CHAIN__ added"; }
}
""".replace("__NETWORKS__", " ".join(NETWORKS))
    .replace("__CHAIN__", CHAIN)
    .replace("__NET__", LINK_LOCAL)
)

SCRIPT = (
    "#!/bin/sh\n"
    "# Lamnezia: the containers of the Amnezia protocols do not reach link-local addresses,\n"
    "# among them the cloud metadata service. The panel rewrites this file and removes it.\n"
    f"{_FUNCTIONS}\n"
    "lamnezia_metadata_apply\n"
)

# The manager logs every run of the service; only the lines of the script about a change pass.
SERVICE = f"""[Unit]
Description=Lamnezia: close the cloud metadata service to the Amnezia containers
After=docker.service

[Service]
Type=oneshot
ExecStart=/bin/sh {SCRIPT_PATH}
SyslogLevel=notice
LogLevelMax=notice
"""

# A reboot or a firewall reload drops the rule: the timer puts it back within a minute.
TIMER = f"""[Unit]
Description=Lamnezia: keep the cloud metadata service closed to the Amnezia containers

[Timer]
OnBootSec=15s
OnUnitActiveSec=60s
AccuracySec=5s
Unit={UNIT}.service

[Install]
WantedBy=timers.target
"""

# What the block puts on the host: (path, content, mode).
FILES = (
    (SCRIPT_PATH, SCRIPT, SCRIPT_MODE),
    (SERVICE_PATH, SERVICE, UNIT_MODE),
    (TIMER_PATH, TIMER, UNIT_MODE),
)

ENABLE = (
    "set -e\n"
    "systemctl daemon-reload\n"
    f"systemctl enable --now {UNIT}.timer >/dev/null 2>&1\n"
    f"sh {SCRIPT_PATH}\n"
)

DISABLE = (
    f"systemctl disable --now {UNIT}.timer >/dev/null 2>&1 || true\n"
    f"systemctl stop {UNIT}.service >/dev/null 2>&1 || true\n"
    f"rm -f {SERVICE_PATH} {TIMER_PATH}\n"
    "systemctl daemon-reload\n"
    f"systemctl reset-failed {UNIT}.service >/dev/null 2>&1 || true\n"
    f"while iptables -D DOCKER-USER -j {CHAIN} 2>/dev/null; do :; done\n"
    f"iptables -F {CHAIN} 2>/dev/null || true\n"
    f"iptables -X {CHAIN} 2>/dev/null || true\n"
    f"rm -rf -- {METADATA_DIR}\n"
)

STATUS = (
    f'echo "{_TIMER_LINE}$(systemctl is-active {UNIT}.timer 2>/dev/null || true)"\n'
    f'if iptables -C DOCKER-USER -j {CHAIN} 2>/dev/null; then echo "{_JUMP_LINE}yes"; '
    f'else echo "{_JUMP_LINE}no"; fi\n'
    f"iptables -S {CHAIN} 2>/dev/null"
    ' | awk \'$1 == "-A" { for (i = 1; i < NF; i++) if ($i == "-i") '
    f'print "{_CLOSED_LINE}" $(i + 1) }}\'\n'
)

_INTERFACE = re.compile(r"^[\w.@-]+$")


@dataclass(frozen=True)
class MetadataReading:
    """What STATUS printed: whether the timer runs, DOCKER-USER leads to the chain of the block,
    and the bridges the chain closes."""

    timer_active: bool
    jumped: bool
    bridges: tuple[str, ...]

    @classmethod
    def parse(cls, text: str) -> Self:
        timer_active = jumped = False
        bridges = []
        for line in map(str.strip, text.splitlines()):
            if line.startswith(_TIMER_LINE):
                timer_active = line.removeprefix(_TIMER_LINE) == "active"
            elif line.startswith(_JUMP_LINE):
                jumped = line.removeprefix(_JUMP_LINE) == "yes"
            elif line.startswith(_CLOSED_LINE):
                bridge = line.removeprefix(_CLOSED_LINE)
                if _INTERFACE.match(bridge):
                    bridges.append(bridge)
        return cls(timer_active, jumped, tuple(bridges))

    @property
    def closed(self) -> bool:
        """The rule works now and the timer keeps it after a reboot."""
        return self.timer_active and self.jumped and bool(self.bridges)
