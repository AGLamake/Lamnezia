"""The amnezia-ipsec container and libreswan (pluto) in it (server_scripts/ipsec/).

Upstream start.sh is empty: configure_container.sh both writes the configuration and starts
pluto and xl2tpd. Its first part (configuration files, firewall, daemons) can run again after a
restart of the container; the part from the IKEv2 marker on makes the certificate authority and
runs only once.
"""

import shlex

from amnezia_core.errors import ConfigFormatError
from amnezia_core.ipsec import (
    CA_NICKNAME,
    CONNECTION_NAME,
    IKEV2_CONFIG_PATH,
    IPSEC_CONTAINER,
    NSS_DATABASE,
    script_variables,
    server_identity,
)
from amnezia_core.script_vars import container_variables, substitute
from apps.operations.services.context import OperationContext
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.container import ProtocolContainer, upstream_file
from apps.ssh.services import CommandResult, RemoteHost

IPSEC_FOLDER = "ipsec"
_PKI_MARKER = "################# IKEV2"
# The script needs bash (`<(...)`). It is uploaded as the app uploads its container scripts:
# given on stdin, it could be read by certutil.
_SCRIPT_PATH = "/opt/amnezia/configure_ipsec.sh"
_SCRIPT_TIMEOUT = 600
_START_WAIT = 30
_WAIT_FOR_PLUTO = (
    "i=0; until ipsec whack --status >/dev/null 2>&1; do "
    f'i=$((i+1)); [ "$i" -ge {_START_WAIT} ] && exit 1; sleep 1; done'
)
_CONNECTION_LOADED = f"ipsec whack --status 2>/dev/null | grep -q '\"{CONNECTION_NAME}\"'"
_HAS_CA = shlex.join(["certutil", "-L", "-d", NSS_DATABASE, "-n", CA_NICKNAME]) + " >/dev/null"
# What an interrupted first setup may leave: a running pluto and a database without the CA.
_RESET = (
    "ipsec whack --shutdown >/dev/null 2>&1; "
    "rm -f /etc/ipsec.d/cert9.db /etc/ipsec.d/key4.db /etc/ipsec.d/pkcs11.txt "
    "/etc/ipsec.d/ca_cert_base64.p12"
)


def configure_script(server_ip: str, *, with_pki: bool) -> str:
    """configure_container.sh for `server_ip`; without the PKI it only starts the services."""
    variables = {**container_variables(IPSEC_CONTAINER), **script_variables(server_ip)}
    script = substitute(upstream_file(f"{IPSEC_FOLDER}/configure_container.sh"), variables)
    if with_pki:
        return script
    services = script.partition(_PKI_MARKER)[0]
    return f"{services}\n{_WAIT_FOR_PLUTO}\nipsec auto --add {CONNECTION_NAME}\n"


class IpsecDocker(ProtocolContainer):
    """amnezia-ipsec on one host: its NSS database, configure_container.sh and pluto."""

    def __init__(self, host: RemoteHost, ctx: OperationContext) -> None:
        super().__init__(host, ctx, IPSEC_CONTAINER)

    def is_configured(self) -> bool:
        """The CA and ikev2.conf are there: the container serves clients once pluto runs."""
        return self.has_file(IKEV2_CONFIG_PATH) and self.shell.sh(_HAS_CA).ok

    def server_ip(self) -> str:
        """The identity of the server certificate, as ikev2.conf names it."""
        try:
            return server_identity(self.shell.read_file(IKEV2_CONFIG_PATH).decode())
        except ConfigFormatError as error:
            raise SetupError(SetupErrorCode.IPSEC_CONFIG_INVALID, str(error)) from error

    def reset(self) -> None:
        """Forget an unfinished setup, so that configure_container.sh makes the CA anew."""
        self.shell.sh(_RESET)

    def run_script(self, script: str) -> CommandResult:
        path = shlex.quote(_SCRIPT_PATH)
        self.shell.write_file_atomic(_SCRIPT_PATH, script.encode(), mode=0o700)
        return self.shell.sh(
            f"bash {path}; code=$?; rm -f {path}; exit $code", timeout=_SCRIPT_TIMEOUT
        )

    def ensure_running(self) -> None:
        """Start pluto with the configuration the container has, if it is not running."""
        if not self.shell.sh(_CONNECTION_LOADED).ok:
            self.run_script(configure_script(self.server_ip(), with_pki=False))
        self.wait_for_pluto()

    def wait_for_pluto(self) -> None:
        wait = f"{_WAIT_FOR_PLUTO}; {_CONNECTION_LOADED}"
        if not self.shell.sh(wait, timeout=_START_WAIT * 2).ok:
            raise SetupError(SetupErrorCode.IPSEC_NOT_RUNNING, IPSEC_CONTAINER)
