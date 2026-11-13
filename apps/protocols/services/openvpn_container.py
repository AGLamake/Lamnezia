"""The amnezia-openvpn container and the openvpn process in it (server_scripts/openvpn/)."""

from amnezia_core.openvpn import OPENVPN_CONTAINER
from apps.operations.services.context import OperationContext
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.container import ProtocolContainer
from apps.ssh.services import RemoteHost

OPENVPN_FOLDER = "openvpn"
# start.sh starts openvpn as a daemon; seconds to wait for it.
_START_WAIT = 30
_OPENVPN_IS_UP = "pgrep openvpn >/dev/null 2>&1"


class OpenVpnDocker(ProtocolContainer):
    """amnezia-openvpn on one host: start.sh runs openvpn after every start, if the CA is made."""

    def __init__(self, host: RemoteHost, ctx: OperationContext) -> None:
        super().__init__(host, ctx, OPENVPN_CONTAINER)

    def wait_for_openvpn(self) -> None:
        wait = (
            f"i=0; until {_OPENVPN_IS_UP}; do "
            f'i=$((i+1)); [ "$i" -ge {_START_WAIT} ] && exit 1; sleep 1; done'
        )
        if not self.shell.sh(wait, timeout=_START_WAIT * 2).ok:
            raise SetupError(SetupErrorCode.OPENVPN_NOT_RUNNING, OPENVPN_CONTAINER)
