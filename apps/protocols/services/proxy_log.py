"""Deleting the 3proxy.log the Amnezia app leaves in SOCKS5: the hosts users asked the proxy for.

Upstream configure_container.sh logs every requested host; the panel writes 3proxy.cfg without
the log (upstream/socks5_proxy/configure_container.sh). The import notes a log left in a proxy
set up in the app, and the server card offers to delete it. 3proxy.cfg is written again as the
panel writes it, with the account it has, so the app keeps working; the proxy restarts so it no
longer holds the file, and only then is the file deleted.
"""

import shlex
from contextlib import ExitStack

from django.utils.translation import gettext as _

from amnezia_core.docker_ps import RunningContainer
from amnezia_core.proxies import SOCKS5_CONFIG_PATH, SOCKS5_LOG_PATH, SOCKS5_PORT, Socks5Account
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.models import InstanceOrigin, ServiceInstance
from apps.protocols.services.container import ProtocolContainer
from apps.protocols.services.proxy_installers import Socks5Installer
from apps.protocols.services.service_import import ServiceFiles, read_text
from apps.ssh.services import RemoteHost

DELETE_LOG_KIND = "protocols.delete_proxy_log"


def start_log_deletion(service: ServiceInstance, user: User, *, ip: str | None = None) -> Operation:
    operation = launch(DELETE_LOG_KIND, {"service_id": service.pk}, user=user, subject=str(service))
    data = {"container": service.container}
    audit.log(DELETE_LOG_KIND, actor=user, target=service.server, data=data, ip=ip)
    return operation


class ProxyLogDeletion:
    """Turns the log of 3proxy off and deletes the file under the server lock."""

    def __init__(self, host: RemoteHost, service: ServiceInstance, ctx: OperationContext) -> None:
        self._host = host
        self._service = service
        self._ctx = ctx

    def run(self) -> None:
        service = self._service
        assert service.is_socks5, service.container
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            docker = ProtocolContainer(self._host, self._ctx, service.container)
            if docker.state() != "running":
                raise SetupError(SetupErrorCode.SOCKS5_NOT_RUNNING, service.container)
            proxy = Socks5Installer(self._host, self._ctx, port=service.port or SOCKS5_PORT)
            with self._ctx.step(_("Turn the proxy log off")):
                if not Socks5Account.from_config(read_text(docker.shell, SOCKS5_CONFIG_PATH)):
                    raise SetupError(SetupErrorCode.SOCKS5_NO_ACCOUNT, service.container)
                proxy.configure()
            with self._ctx.step(_("Restart the proxy")):
                self._host.docker("restart", service.container)
            with self._ctx.step(_("Delete 3proxy.log")):
                docker.shell.run(f"rm -f -- {shlex.quote(SOCKS5_LOG_PATH)}")
            with self._ctx.step(_("Read the service")):
                running = RunningContainer(service.container, proxy.port, str(proxy.transport))
                origin = InstanceOrigin(service.origin)
                ServiceFiles.read(self._host, running).save(service.server, origin)
