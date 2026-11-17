"""A WireGuard-family container and its interface, as the upstream server scripts drive them.

amnezia-awg2 (server_scripts/awg/), amnezia-awg (awg_legacy/) and amnezia-wireguard
(wireguard/). Shared by the installer, the container actions and the change of the protocol
settings.
"""

import shlex
from ipaddress import IPv4Network

from django.utils.translation import gettext as _

from amnezia_core.awg_quick import UpOutput, UpRefusal
from amnezia_core.containers import AWG2, AWG_LEGACY, WIREGUARD, WgContainer
from amnezia_core.script_vars import substitute
from apps.operations.services.context import OperationContext
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.container import (
    ProtocolContainer,
    log_arguments,
    upstream_file,
)
from apps.ssh.services import RemoteHost

AWG_FOLDER = "awg"
# Upstream server_scripts/<folder>/ of each container, copied to apps/protocols/upstream/.
SCRIPT_FOLDERS = {AWG2.name: AWG_FOLDER, AWG_LEGACY.name: "awg_legacy", WIREGUARD.name: "wireguard"}
START_SCRIPT = "/opt/amnezia/start.sh"
# start.sh brings the interface up in the background; seconds to wait for it.
_START_WAIT = 30
# What a refusal of `<tool>-quick up` means for the administrator.
_REFUSALS = {
    UpRefusal.UNKNOWN_PARAMETER: SetupErrorCode.AWG_IMAGE_OUTDATED,
    UpRefusal.INVALID_ARGUMENT: SetupErrorCode.AWG_PADDING_REFUSED,
    UpRefusal.OTHER: SetupErrorCode.AWG_NOT_RUNNING,
}


def upstream_script(name: str, kind: WgContainer = AWG2) -> str:
    return upstream_file(f"{SCRIPT_FOLDERS[kind.name]}/{name}")


def run_arguments(port: int, *, logs: bool, kind: WgContainer = AWG2) -> list[str]:
    """`docker run` options of upstream run_container.sh; `logs` keeps the container log."""
    return [
        "docker", "run", "-d",
        *log_arguments(logs=logs),
        "--restart", "always",
        "--privileged",
        "--cap-add=NET_ADMIN",
        "--cap-add=SYS_MODULE",
        "-p", f"{port}:{port}/udp",
        "-v", "/lib/modules:/lib/modules",
        "--sysctl=net.ipv4.conf.all.src_valid_mark=1",
        "--name", kind.name,
        kind.name,
    ]  # fmt: skip


class AwgContainer(ProtocolContainer):
    """A WireGuard-family container on one host, with its interface inside it."""

    def __init__(self, host: RemoteHost, ctx: OperationContext, kind: WgContainer = AWG2) -> None:
        super().__init__(host, ctx, kind.name)
        self.kind = kind

    def build_image(self) -> None:
        self.build(upstream_script("Dockerfile", self.kind))

    def run(self, port: int, *, logs: bool = False) -> None:
        """Start a new container with the UDP port published; there must be none yet."""
        self.run_container(run_arguments(port, logs=logs, kind=self.kind))

    def start_awg(self, subnet: IPv4Network) -> None:
        """Write the real start.sh if the container has the placeholder, then run it.

        The Dockerfile puts a placeholder start.sh into the image; the real one runs
        `<tool>-quick`.
        """
        if self._quick not in self.shell.read_file(START_SCRIPT).decode():
            variables = {
                "$AWG_SUBNET_IP": str(subnet.network_address),
                "$WIREGUARD_SUBNET_IP": str(subnet.network_address),
                "$WIREGUARD_SUBNET_CIDR": str(subnet.prefixlen),
            }
            script = substitute(upstream_script("start.sh", self.kind), variables)
            self.shell.write_file_atomic(START_SCRIPT, script.encode(), mode=0o755)
        if self.awg_is_up():
            self._ctx.log(_("The VPN interface is already up."))
            return
        command = f"chmod a+x {START_SCRIPT} && {START_SCRIPT}"
        self._host.docker("exec", "-d", self.name, "sh", "-c", command)
        self.wait_for_awg()

    def wait_for_awg(self) -> None:
        """Wait until start.sh, run in the background, brings the interface up with its settings.

        The background run prints nowhere: if the interface does not come up, `<tool>-quick up`
        runs once more to tell why.
        """
        wait = (
            f"i=0; until {self._show} >/dev/null 2>&1; do "
            f'i=$((i+1)); [ "$i" -ge {_START_WAIT} ] && exit 1; sleep 1; done'
        )
        if not self.shell.sh(wait, timeout=_START_WAIT * 2).ok:
            self._up()
        self.check_header_protection()

    def awg_is_up(self) -> bool:
        return self.shell.sh(f"{self._show} >/dev/null 2>&1").ok

    def restart_awg(self, *, check: bool = True) -> None:
        """Bring the interface down and up with its config; SetupError if it does not come up.

        `check`: the interface must also run with the AmneziaWG 3.1 settings of the config.
        """
        self.shell.sh(f"{self._quick} down {shlex.quote(self.kind.config_path)}")
        self._up()
        if check:
            self.check_header_protection()

    def check_header_protection(self) -> None:
        """The interface runs with the AmneziaWG 3.1 settings of its config, if it has them.

        An amneziawg kernel module older than 3.0 drops them without an error: `awg show` then
        has no header protection key. Nothing is printed, the key stays out of the log.
        """
        conf = shlex.quote(self.kind.config_path)
        script = (
            f"! grep -q '^[[:space:]]*HeaderProtectionKey' {conf} || "
            f"{self._show} 2>/dev/null | grep -q 'header protection key'"
        )
        if not self.shell.sh(script).ok:
            raise SetupError(SetupErrorCode.AWG3_DROPPED, self.kind.interface)

    def _up(self) -> None:
        """`<tool>-quick up`; a refusal is logged without config values and explained."""
        result = self.shell.sh(f"{self._quick} up {shlex.quote(self.kind.config_path)}")
        if result.ok and self.awg_is_up():
            return
        output = UpOutput.parse(f"{result.output}\n{result.errors}")
        for line in output.lines:
            self._ctx.log(line)
        raise SetupError(_REFUSALS[output.refusal], self.kind.interface)

    @property
    def _quick(self) -> str:
        return f"{self.kind.tool}-quick"

    @property
    def _show(self) -> str:
        return f"{self.kind.tool} show {self.kind.interface}"
