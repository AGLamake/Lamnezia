"""Shell script and systemd units that close the cloud metadata service to the VPN clients.

A VPN client goes out to the network through the host, so it can reach 169.254.169.254: the
metadata service of a cloud server, which hands out the tokens of the server's service account
on AWS, GCP, Azure and Yandex Cloud (AWS also answers on fd00:ec2::254). The script drops
traffic from the Docker bridges of the Amnezia containers (the default `bridge` and
`amnezia-dns-net`) to the whole link-local range 169.254.0.0/16 and to the IPv6 address:

- Docker with iptables (the default): a chain of the block, which the DOCKER-USER chain leads
  to, in iptables and, if Docker keeps ip6tables for IPv6 containers, in ip6tables too;
- Docker with nftables (`firewall-backend: nftables`, Docker 28+) has no DOCKER-USER: a table of
  the block with its own forward chain, as Docker advises; a drop there is final.

The containers do not change, and the host itself still reaches the metadata: its own traffic
does not pass the forward hook. Rules are lost on reboot and a firewall reload flushes them, so
a systemd timer runs the script again; the script adds only what is missing. `VERSION` counts
the changes of the script, so the panel brings it to the servers closed by an older one.
"""

import re
from dataclasses import dataclass
from typing import Self

from apps.protocols.services.host_scripts import AMNEZIA_NETWORK

# Raise when SCRIPT changes: servers with an older script get the new one (metadata_block.py).
VERSION = 2

UNIT = "lamnezia-metadata"
CHAIN = "LAMNEZIA-METADATA"
TABLE = "lamnezia_metadata"
LINK_LOCAL = "169.254.0.0/16"
METADATA_IPV6 = "fd00:ec2::254"
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

# $1 is iptables or ip6tables, $2 the addresses closed. Fails without DOCKER-USER: Docker makes
# it when it starts with the iptables backend.
lamnezia_metadata_iptables() {
  "$1" -n -L DOCKER-USER >/dev/null 2>&1 || return 1
  "$1" -n -L __CHAIN__ >/dev/null 2>&1 || "$1" -N __CHAIN__
  for dev in $bridges; do
    "$1" -C __CHAIN__ -i "$dev" -d "$2" -j DROP 2>/dev/null && continue
    "$1" -A __CHAIN__ -i "$dev" -d "$2" -j DROP && echo "$dev: $2 closed"
  done
  "$1" -C DOCKER-USER -j __CHAIN__ 2>/dev/null \
    || { "$1" -I DOCKER-USER -j __CHAIN__ && echo "$1 DOCKER-USER: __CHAIN__ added"; }
}

# Docker with the nftables backend keeps its rules in the docker-bridges tables.
lamnezia_metadata_nft() {
  command -v nft >/dev/null 2>&1 || return 0
  nft list table ip docker-bridges >/dev/null 2>&1 || return 0
  if ! nft list chain inet __TABLE__ forward >/dev/null 2>&1; then
    nft add table inet __TABLE__
    nft add chain inet __TABLE__ forward '{ type filter hook forward priority -1; policy accept; }'
    echo "nftables: __TABLE__ added"
  fi
  rules=$(nft list chain inet __TABLE__ forward)
  for dev in $bridges; do
    for match in "ip daddr __NET__" "ip6 daddr __NET6__"; do
      case $rules in *"iifname \"$dev\" $match drop"*) continue ;; esac
      nft add rule inet __TABLE__ forward iifname "$dev" $match drop \
        && echo "$dev: ${match##* } closed"
    done
  done
}

lamnezia_metadata_apply() {
  bridges=$(lamnezia_metadata_bridges)
  if lamnezia_metadata_iptables iptables __NET__; then
    lamnezia_metadata_iptables ip6tables __NET6__ || true
  else
    lamnezia_metadata_nft
  fi
}
""".replace("__NETWORKS__", " ".join(NETWORKS))
    .replace("__CHAIN__", CHAIN)
    .replace("__TABLE__", TABLE)
    .replace("__NET6__", METADATA_IPV6)
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
    "for ipt in iptables ip6tables; do\n"
    f'  while "$ipt" -D DOCKER-USER -j {CHAIN} 2>/dev/null; do :; done\n'
    f'  "$ipt" -F {CHAIN} 2>/dev/null || true\n'
    f'  "$ipt" -X {CHAIN} 2>/dev/null || true\n'
    "done\n"
    f"nft delete table inet {TABLE} 2>/dev/null || true\n"
    f"rm -rf -- {METADATA_DIR}\n"
)

# `jump` says that a forward hook leads to the rules: DOCKER-USER to the chain, or the forward
# chain of the table; `closed` lines name the bridges closed to 169.254.0.0/16.
STATUS = (
    f'echo "{_TIMER_LINE}$(systemctl is-active {UNIT}.timer 2>/dev/null || true)"\n'
    f"if iptables -C DOCKER-USER -j {CHAIN} 2>/dev/null"
    f" || nft list chain inet {TABLE} forward >/dev/null 2>&1;"
    f' then echo "{_JUMP_LINE}yes"; else echo "{_JUMP_LINE}no"; fi\n'
    f"iptables -S {CHAIN} 2>/dev/null"
    ' | awk \'$1 == "-A" { for (i = 1; i < NF; i++) if ($i == "-i") '
    f'print "{_CLOSED_LINE}" $(i + 1) }}\'\n'
    f"nft list chain inet {TABLE} forward 2>/dev/null"
    ' | awk \'$1 == "iifname" && $3 == "ip" { gsub(/"/, "", $2); '
    f'print "{_CLOSED_LINE}" $2 }}\'\n'
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
