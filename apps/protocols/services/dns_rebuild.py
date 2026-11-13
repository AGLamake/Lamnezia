"""Rebuilding AmneziaDNS the Amnezia app installed, so its config turns the query logs off.

The app builds amnezia-dns from its own Dockerfile, whose forward-records.conf says nothing about
the logs; the panel's Dockerfile turns them off there (upstream/dns/Dockerfile). The import notes
a DNS container without these lines, and the server card offers to rebuild it. The new image is
built first, while the old container still answers; then the old container is set aside and a
new one runs as upstream run_container.sh starts it. If it does not run, the old one comes back.
"""

from contextlib import ExitStack

from django.utils.translation import gettext as _

from amnezia_core.docker_ps import RunningContainer
from amnezia_core.services import DNS_CONTAINER, DNS_PORT
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.models import InstanceOrigin, ServiceInstance
from apps.protocols.services.container import ProtocolContainer, upstream_file
from apps.protocols.services.container_swap import ContainerSwap
from apps.protocols.services.service_import import ServiceFiles
from apps.protocols.services.service_installers import DnsInstaller
from apps.ssh.services import RemoteHost

REBUILD_DNS_KIND = "protocols.rebuild_dns"


def start_dns_rebuild(service: ServiceInstance, user: User, *, ip: str | None = None) -> Operation:
    params = {"service_id": service.pk}
    operation = launch(REBUILD_DNS_KIND, params, user=user, subject=str(service))
    data = {"container": service.container}
    audit.log(REBUILD_DNS_KIND, actor=user, target=service.server, data=data, ip=ip)
    return operation


class DnsRebuild:
    """A new amnezia-dns from the panel's Dockerfile in place of the old one, under the lock."""

    def __init__(self, host: RemoteHost, service: ServiceInstance, ctx: OperationContext) -> None:
        self._host = host
        self._service = service
        self._ctx = ctx

    def run(self) -> None:
        service = self._service
        assert service.container == DNS_CONTAINER, service.container
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            docker = ProtocolContainer(self._host, self._ctx, DNS_CONTAINER)
            if docker.state() != "running":
                raise SetupError(SetupErrorCode.DNS_NOT_RUNNING, DNS_CONTAINER)
            installer = DnsInstaller(self._host, self._ctx, port=DNS_PORT, logs=docker.keeps_log())
            with self._ctx.step(_("Build the image")):
                docker.build(upstream_file("dns/Dockerfile"))
            with ContainerSwap(self._host, self._ctx, docker).replace():
                docker.run_container(installer.run_arguments())
                if docker.state() != "running":
                    raise SetupError(SetupErrorCode.CONTAINER_START_FAILED, DNS_CONTAINER)
            with self._ctx.step(_("Read the service")):
                running = RunningContainer(DNS_CONTAINER, DNS_PORT, str(installer.transport))
                ServiceFiles.read(self._host, running).save(
                    service.server, InstanceOrigin(service.origin)
                )
