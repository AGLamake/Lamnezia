"""The amnezia-xray container and the xray process in it (upstream server_scripts/xray/)."""

from amnezia_core.xray import XRAY_CONTAINER
from apps.operations.services.context import OperationContext
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.container import ProtocolContainer
from apps.ssh.services import RemoteHost

XRAY_FOLDER = "xray"
# start.sh starts xray in the background; seconds to wait for it.
_START_WAIT = 30
_XRAY_IS_UP = "pgrep xray >/dev/null 2>&1"


class XrayDocker(ProtocolContainer):
    """amnezia-xray on one host: start.sh runs xray with server.json after every start."""

    def __init__(self, host: RemoteHost, ctx: OperationContext) -> None:
        super().__init__(host, ctx, XRAY_CONTAINER)

    def xray_is_up(self) -> bool:
        return self.shell.sh(_XRAY_IS_UP).ok

    def wait_for_xray(self) -> None:
        wait = (
            f"i=0; until {_XRAY_IS_UP}; do "
            f'i=$((i+1)); [ "$i" -ge {_START_WAIT} ] && exit 1; sleep 1; done'
        )
        if not self.shell.sh(wait, timeout=_START_WAIT * 2).ok:
            raise SetupError(SetupErrorCode.XRAY_NOT_RUNNING, XRAY_CONTAINER)
