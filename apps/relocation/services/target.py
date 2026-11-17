"""The new machine of a panel move: checks, the upload, the installer and the copy's state."""

import hashlib
import ipaddress
import re
import shlex
from pathlib import Path

from django.conf import settings
from django.utils.translation import gettext as _

from apps.operations.services.context import OperationContext
from apps.relocation.errors import MoveError, MoveErrorCode
from apps.relocation.models import MoveState
from apps.relocation.services.panel_machine import PanelMachine
from apps.ssh.services import RemoteHost

INSTALL_DIR = "/opt/lamnezia"
# Written before anything else of the copy: only a directory with it is removed by a cancel.
MARKER = f"{INSTALL_DIR}/.moving"
NGINX_SITE = "/etc/nginx/sites-available/lamnezia"
# Stdin of one SSH command goes to the server in one piece, so big files go in chunks.
_CHUNK = 4 * 1024 * 1024
_CADDY_PORTS = (("tcp", 80), ("tcp", 443), ("udp", 443))
# `ss -Hlntu` columns: proto, state, recv-q, send-q, local address:port, peer.
_SS_LOCAL = 4
_PUBLISHED = re.compile(r":(\d+)->\d+/(tcp|udp)")
_COLOR = re.compile(r"\x1b\[[0-9;]*m")
_INSTALL_LOG_PREFIXES = ("==>", "Error:")
_FAILURE_TAIL = 15
# Addresses of the machine: its interfaces and its containers (the panel may be one of them).
_OWN_ADDRESSES = """
ip -o addr show 2>/dev/null | awk '{print $4}'
if command -v docker >/dev/null; then
    docker ps -q | xargs -r docker inspect \
        -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' 2>/dev/null
fi
true
"""

# Takes back everything a move installed; install.sh writes the stop timer and, with --moving,
# the certificate timer.
_DISCARD = f"""
dir={INSTALL_DIR}
[ -f "$dir/.moving" ] || exit 0
if [ -f "$dir/.env" ] && command -v docker >/dev/null; then
    (cd "$dir" && docker compose --profile caddy down --volumes --remove-orphans --rmi local) ||
        exit 1
fi
if command -v systemctl >/dev/null; then
    for unit in lamnezia-certificate lamnezia-stop; do
        systemctl disable --now "$unit.timer" 2>/dev/null
        rm -f "/etc/systemd/system/$unit.timer" "/etc/systemd/system/$unit.service"
    done
    systemctl daemon-reload
fi
if [ -e {NGINX_SITE} ]; then
    rm -f /etc/nginx/sites-enabled/lamnezia {NGINX_SITE}
    nginx -t 2>/dev/null && systemctl reload nginx
fi
rm -rf "$dir"
"""


class TargetMachine:
    """The machine the panel moves to, as a root shell over SSH."""

    def __init__(self, host: RemoteHost, ctx: OperationContext, move_id: int) -> None:
        self._host = host
        self._ctx = ctx
        self._move_id = move_id
        self._work_dir = f"/var/tmp/lamnezia-move-{move_id}"

    def check(self) -> None:
        """Refuse a machine the installer cannot use; a copy left by an earlier move goes."""
        self._host.check_privileges()
        self._check_not_the_panel()
        if not self._host.sh("command -v apt-get").ok:
            raise MoveError(MoveErrorCode.UNSUPPORTED_SYSTEM)
        if self._host.sh(f"[ -f {MARKER} ]").ok:
            self._ctx.log(_("A copy left by an earlier move is removed."))
            self.discard()
        if self._host.sh(f"[ -e {INSTALL_DIR} ] || [ -e {NGINX_SITE} ]").ok:
            raise MoveError(MoveErrorCode.TARGET_IN_USE)
        self._check_disk()
        if self._host.sh("command -v nginx").ok:
            self._ctx.log(_("nginx is installed: the panel adds a site to it."))
        else:
            self._check_caddy_ports()
        if not self._host.sh("docker compose version").ok:
            self._ctx.log(
                _("Docker with compose is missing: the installer adds it, containers restart once.")
            )

    def upload_code(self, bundle: Path) -> None:
        remote = f"{self._work_dir}/code.tar.gz"
        self._upload(bundle, remote)
        self._host.run(
            f"set -e; install -d -m 755 {INSTALL_DIR}; : > {MARKER}; "
            f"tar -xzf {remote} -C {INSTALL_DIR} --no-same-owner; rm -f {remote}"
        )

    def upload_archive(self, archive: Path) -> str:
        remote = f"{self._work_dir}/panel.lamnezia"
        self._upload(archive, remote)
        return remote

    def install(self, archive: str, password: str) -> None:
        """Run install.sh with the archive; the password goes through stdin only."""
        script = (
            "IFS= read -r LAMNEZIA_ARCHIVE_PASSWORD\n"
            "export LAMNEZIA_ARCHIVE_PASSWORD\n"
            f"exec bash {INSTALL_DIR}/install.sh --dir {INSTALL_DIR} "
            f"--restore {shlex.quote(archive)} --moving 2>&1"
        )
        result = self._host.sh(
            script, stdin=f"{password}\n".encode(), timeout=settings.RELOCATION_INSTALL_TIMEOUT
        )
        lines = _COLOR.sub("", result.output).splitlines()
        shown = [line for line in lines if line.startswith(_INSTALL_LOG_PREFIXES)]
        for line in shown if result.ok else lines[-_FAILURE_TAIL:]:
            self._ctx.log(line)
        if not result.ok:
            raise MoveError(MoveErrorCode.INSTALL_FAILED, f"exit {result.exit_code}")

    def verify(self, expected_state: str) -> None:
        """The copy answers its health check and holds the move in the expected state."""
        self._check_health(expected_state)
        self._ctx.log(_("The copy answers and accepts no changes yet."))

    def verify_live(self) -> None:
        """The copy answers its health check and accepts changes: the move arrived there."""
        self._check_health(MoveState.ARRIVED)
        self._ctx.log(_("The copy on the new machine answers and accepts changes."))

    def copy_state(self) -> str | None:
        """The state of this move in the copy's database; None when the copy does not answer."""
        result = self._host.sh(self._manage("status"))
        return result.output.strip() if result.ok else None

    def arrive(self) -> None:
        """Let the copy accept changes; after that a cancel no longer removes it."""
        self._host.run(self._manage("arrive"))
        self._host.run(f"rm -f {MARKER}")

    def discard(self) -> None:
        """Remove the copy, its nginx site and certificate timer; only with the marker."""
        result = self._host.sh(_DISCARD, timeout=settings.RELOCATION_INSTALL_TIMEOUT)
        if not result.ok:
            raise MoveError(MoveErrorCode.NOT_DISCARDED, result.errors.strip())

    def remove_uploads(self) -> None:
        self._host.sh(f"rm -rf {self._work_dir}")

    def _check_health(self, expected_state: str) -> None:
        health = self._host.sh(
            f"cd {INSTALL_DIR} && bind=$(sed -n 's/^LAMNEZIA_HTTP_BIND=//p' .env) && "
            "curl -fsS -H 'Host: localhost' \"http://$bind/healthz\""
        )
        if not health.ok:
            raise MoveError(MoveErrorCode.NOT_STARTED, health.errors.strip())
        state = self.copy_state()
        if state != expected_state:
            raise MoveError(MoveErrorCode.NOT_STARTED, f"state {state!r}")

    def _manage(self, command: str) -> str:
        return (
            f"cd {INSTALL_DIR} && docker compose exec -T web "
            f"python manage.py relocation {command} {self._move_id}"
        )

    def _check_not_the_panel(self) -> None:
        """Refuse the panel's own machine: the panel connects from it or its domain points there."""
        panel = _addresses([self._host.client_address() or "", *PanelMachine.current().addresses])
        if panel & _addresses(self._host.run(_OWN_ADDRESSES).split()):
            raise MoveError(MoveErrorCode.SAME_MACHINE)

    def _check_disk(self) -> None:
        free = self._host.run("df -Pm / | awk 'NR == 2 {print $4}'").strip()
        if not free.isdigit() or int(free) < settings.RELOCATION_MIN_FREE_MB:
            raise MoveError(MoveErrorCode.LOW_DISK, f"{free} MB")
        self._ctx.log(_("Free disk space: %(size)s MB.") % {"size": free})

    def _check_caddy_ports(self) -> None:
        """Caddy of the panel needs 80 and 443; a relay never takes them (RESERVED_PORTS)."""
        busy: set[tuple[str, int]] = set()
        for line in self._host.run("ss -Hlntu").splitlines():
            fields = line.split()
            local = fields[_SS_LOCAL] if len(fields) > _SS_LOCAL else ""
            port = local.rpartition(":")[2]
            if port.isdigit():
                busy.add((fields[0], int(port)))
        docker = self._host.sh("docker ps --format '{{.Names}} {{.Ports}}'")
        if docker.ok:
            busy |= {(proto, int(port)) for port, proto in _PUBLISHED.findall(docker.output)}
        taken = [f"{port}/{proto}" for proto, port in _CADDY_PORTS if (proto, port) in busy]
        if taken:
            self._ctx.log(_("Taken: %(ports)s.") % {"ports": ", ".join(taken)})
            raise MoveError(MoveErrorCode.PORTS_BUSY, ", ".join(taken))

    def _upload(self, local: Path, remote: str) -> None:
        self._host.run(f"install -d -m 700 {self._work_dir} && : > {remote}")
        digest = hashlib.sha256()
        with local.open("rb") as source:
            while chunk := source.read(_CHUNK):
                digest.update(chunk)
                self._host.run(f"cat >> {remote}", stdin=chunk)
        written = self._host.run(f"sha256sum {remote}").split()
        if not written or written[0] != digest.hexdigest():
            raise MoveError(MoveErrorCode.UPLOAD_FAILED, remote)


def _addresses(values: list[str]) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """IP addresses among the values («10.0.0.5/24» counts), without loopback and link-local."""
    found = set()
    for value in values:
        try:
            address = ipaddress.ip_address(value.split("/")[0].split("%")[0])
        except ValueError:
            continue
        if not (address.is_loopback or address.is_link_local or address.is_unspecified):
            found.add(address)
    return found
