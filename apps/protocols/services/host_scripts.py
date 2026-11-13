"""Shell scripts that prepare a Debian or Ubuntu host for Amnezia containers.

Adapted from amnezia-client client/server_scripts/install_docker.sh, prepare_host.sh and
setup_host_firewall.sh. Copyright (C) Amnezia VPN contributors, GPL-3.0 (see NOTICE).
Only the apt branch is kept; every script checks the state first, so it is safe to run again.
"""

import shlex

OS_RELEASE_PATH = "/etc/os-release"
SUPPORTED_DISTRIBUTIONS = frozenset({"debian", "ubuntu"})

# Commands Docker and the Amnezia app scripts need, and the Debian packages that provide them.
REQUIRED_COMMANDS = ("docker", "fuser", "lsof")
APPARMOR_COMMAND = "apparmor_parser"
PACKAGES = {
    "docker": "docker.io",
    "fuser": "psmisc",
    "lsof": "lsof",
    APPARMOR_COMMAND: "apparmor",
}

# Prints the commands from REQUIRED_COMMANDS that are missing; apparmor_parser only when
# AppArmor is enabled in the kernel (Docker cannot start containers without it then).
MISSING_COMMANDS = (
    f"for cmd in {' '.join(REQUIRED_COMMANDS)}; do\n"
    '  command -v "$cmd" >/dev/null 2>&1 || echo "$cmd"\n'
    "done\n"
    'if [ "$(cat /sys/module/apparmor/parameters/enabled 2>/dev/null)" = "Y" ]; then\n'
    f"  command -v {APPARMOR_COMMAND} >/dev/null 2>&1 || echo {APPARMOR_COMMAND}\n"
    "fi\n"
)
# Upstream install_conntrack.sh, for the containers that need it.
CONNTRACK_PACKAGE = "conntrack"

# apt waits this long for automatic updates to release the dpkg lock (upstream: 30 x 10 s).
APT_LOCK_TIMEOUT = 300
# Seconds for the whole package installation on a slow VPS.
APT_TIMEOUT = 900
APT_LOCK_MESSAGES = ("Could not get lock", "Unable to acquire the dpkg frontend lock")

# `enable --now` also starts Docker after a reboot; the last line is the service state.
DOCKER_SERVICE = "systemctl enable --now docker >/dev/null 2>&1\nsystemctl is-active docker\n"
DOCKER_ACTIVE = "active"

# The network Amnezia containers and the Amnezia DNS container are attached to.
AMNEZIA_NETWORK = "amnezia-dns-net"
AMNEZIA_NETWORK_SUBNET = "172.29.172.0/24"
AMNEZIA_NETWORK_CREATE = (
    "network",
    "create",
    "--driver",
    "bridge",
    f"--subnet={AMNEZIA_NETWORK_SUBNET}",
    "--opt",
    "com.docker.network.bridge.name=amn0",
    AMNEZIA_NETWORK,
)

_FORWARD_RULES = (
    "-j DOCKER-USER",
    "-j DOCKER-ISOLATION-STAGE-1",
    "-o docker0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
    "-o docker0 -j DOCKER",
    "-i docker0 ! -o docker0 -j ACCEPT",
    "-i docker0 -o docker0 -j ACCEPT",
)
_SYSCTL = (
    "net.ipv4.ip_forward=1",
    # Telegram proxies open hundreds of connections; the default conntrack table overflows.
    "net.netfilter.nf_conntrack_max=262144",
    "fs.file-max=51200",
    "net.core.rmem_max=67108864",
    "net.core.wmem_max=67108864",
    "net.core.netdev_max_backlog=250000",
    "net.core.somaxconn=4096",
    "net.ipv4.tcp_syncookies=1",
    "net.ipv4.tcp_tw_reuse=1",
    "net.ipv4.tcp_tw_recycle=0",
    "net.ipv4.tcp_fin_timeout=30",
    "net.ipv4.tcp_keepalive_time=1200",
    "net.ipv4.ip_local_port_range=10000 65000",
    "net.ipv4.tcp_max_syn_backlog=8192",
    "net.ipv4.tcp_max_tw_buckets=5000",
    "net.ipv4.tcp_fastopen=3",
    "net.ipv4.tcp_mem=25600 51200 102400",
    "net.ipv4.tcp_rmem=4096 87380 67108864",
    "net.ipv4.tcp_wmem=4096 65536 67108864",
    "net.ipv4.tcp_mtu_probing=1",
    "net.ipv4.tcp_congestion_control=hybla",
)


def install_packages(packages: list[str]) -> str:
    apt = f"apt-get -o DPkg::Lock::Timeout={APT_LOCK_TIMEOUT} -yq"
    return (
        "set -e\n"
        "export DEBIAN_FRONTEND=noninteractive LC_ALL=C\n"
        f"{apt} update\n"
        f"{apt} install --install-recommends {shlex.join(packages)}\n"
    )


def network_tuning_commands() -> list[str]:
    """Firewall rules and kernel settings, each one idempotent on its own."""
    rule = "iptables -C INPUT -p icmp --icmp-type echo-request -j DROP"
    commands = [f"{rule} 2>/dev/null || {rule.replace(' -C ', ' -A ', 1)}"]
    commands += [
        f"iptables -C FORWARD {spec} 2>/dev/null || iptables -A FORWARD {spec}"
        for spec in _FORWARD_RULES
    ]
    commands += [f"sysctl -w {shlex.quote(setting)}" for setting in _SYSCTL]
    return commands


def tolerant(commands: list[str]) -> str:
    """Run every command and print the ones that failed, like upstream which ignores failures.

    Old or container kernels lack some settings (tcp_tw_recycle, hybla); that is not an error.
    """
    return "".join(
        f"{{ {command}; }} >/dev/null 2>&1 || echo {shlex.quote(command)}\n" for command in commands
    )
