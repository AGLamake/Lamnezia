"""Restart, stop and removal of a protocol container, and a look at its log (DESIGN.md 6.1).

Service containers (SFTP, the Tor site, DNS, the proxies) are restarted, stopped and read the
same way. Changes run under the server lock, so they do not interleave with key operations. Removal
follows upstream server_scripts/remove_container.sh: the clients go away with the container.
The panel then either keeps the instance, its keys and backup for a restore, or revokes the keys
and forgets the instance with its backup.
"""

import shlex
from contextlib import ExitStack
from functools import partial

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from amnezia_core.containers import WG_CONTAINERS
from amnezia_core.ipsec import IPSEC_CONTAINER
from amnezia_core.openvpn import OPENVPN_CONTAINER
from amnezia_core.xray import XRAY_CONTAINER
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.signals import xray_restarting
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.models import ProtocolInstance, ServiceInstance
from apps.protocols.services.awg_container import AwgContainer
from apps.protocols.services.container import ProtocolContainer
from apps.protocols.services.installer import installers
from apps.protocols.services.ipsec_container import IpsecDocker
from apps.protocols.services.openvpn_container import OpenVpnDocker
from apps.protocols.services.xray_container import XrayDocker
from apps.protocols.signals import container_removing, keys_revoked
from apps.ssh.services import RemoteHost

RESTART_KIND = "protocols.restart_container"
STOP_KIND = "protocols.stop_container"
REMOVE_KIND = "protocols.remove_container"
LOGS_KIND = "protocols.container_logs"
RESTART_SERVICE_KIND = "protocols.restart_service"
STOP_SERVICE_KIND = "protocols.stop_service"
SERVICE_LOGS_KIND = "protocols.service_logs"
CHANGE_KINDS = (RESTART_KIND, STOP_KIND, REMOVE_KIND, RESTART_SERVICE_KIND, STOP_SERVICE_KIND)
# Lines of `docker logs` shown in the operation.
LOG_LINES = 200
# A container run with `--log-driver none` (as Amnezia does); docker logs then answers with this.
_NO_LOG_MARKER = "does not support reading"
_STATE_FORMAT = (
    "{{.State.Status}} · started {{.State.StartedAt}} · exit code {{.State.ExitCode}}"
    " · restarts {{.RestartCount}}"
)


def start_container_action(
    kind: str, instance: ProtocolInstance, user: User, **options: bool
) -> Operation:
    """`options` go to the operation as they are: `delete_backups` for a removal."""
    params = {"instance_id": instance.pk, **options}
    operation = launch(kind, params, user=user, subject=str(instance))
    if kind in CHANGE_KINDS:
        data = {"container": instance.container, **options}
        audit.log(kind, actor=user, target=instance, data=data)
    return operation


def start_service_action(kind: str, service: ServiceInstance, user: User) -> Operation:
    operation = launch(kind, {"service_id": service.pk}, user=user, subject=str(service))
    if kind in CHANGE_KINDS:
        audit.log(kind, actor=user, target=service.server, data={"container": service.container})
    return operation


class ContainerControl:
    """Restart, stop and log of one container on its host; changes wait for the server lock.

    Used as it is for service containers: their start script runs again on a restart.
    """

    no_log_hint = gettext_lazy(
        "The container keeps no log: it runs with logging turned off. Install it again with "
        "the container log on."
    )

    def __init__(self, host: RemoteHost, name: str, ctx: OperationContext) -> None:
        self._host = host
        self._name = name
        self._ctx = ctx

    def restart(self) -> None:
        """Restart a running container or start a stopped one."""
        with self._locked():
            self._before_down()
            with self._ctx.step(_("Restart the container")):
                self._host.docker("restart", self._name)
            self._wait_until_serving()

    def stop(self) -> None:
        with self._locked():
            self._before_down()
            with self._ctx.step(_("Stop the container")):
                self._host.docker("stop", self._name)

    def show_log(self) -> None:
        with self._ctx.step(_("Read the container state")):
            self._ctx.log(self._host.docker("inspect", "-f", _STATE_FORMAT, self._name).strip())
        with self._ctx.step(_("Read the container log")):
            command = ["docker", "logs", "--tail", str(LOG_LINES), self._name]
            result = self._host.sh(f"{shlex.join(command)} 2>&1")
            if _NO_LOG_MARKER in result.output:
                self._ctx.log(str(self.no_log_hint))
                return
            self._ctx.log(result.output.rstrip() or _("The log is empty."))

    def _before_down(self) -> None:
        """Nothing to save by default before the container goes down."""

    def _wait_until_serving(self) -> None:
        """Nothing to wait for by default: docker reports the container running."""

    def _locked(self) -> ExitStack:
        stack = ExitStack()
        with self._ctx.step(_("Wait for other operations on the server")):
            stack.enter_context(self._host.lock())
        return stack


class ContainerLifecycle(ContainerControl):
    """The container of one protocol instance: a restart waits until the VPN serves again."""

    no_log_hint = gettext_lazy(
        "The container keeps no log: it runs with logging turned off, as Amnezia starts it. "
        "Turn on the container log in the protocol settings."
    )

    def __init__(self, host: RemoteHost, instance: ProtocolInstance, ctx: OperationContext) -> None:
        super().__init__(host, instance.container, ctx)
        self.instance = instance
        self._docker = ProtocolContainer(host, ctx, self._name)

    def remove(self, *, keep_keys: bool = False) -> None:
        """Remove the container and its image; a container that is already gone is no error.

        With `keep_keys` the backups take a last copy of the container files first.
        """
        with self._locked(), self._ctx.step(_("Remove the container")):
            if not self._docker.state(self._name):
                self._ctx.log(_("The server has no such container any more."))
                return
            if keep_keys:
                container_removing.send(
                    ProtocolInstance, instance=self.instance, host=self._host, ctx=self._ctx
                )
            installer = installers.find(self._name)
            self._docker.remove_with_image(data_volume=bool(installer and installer.data_volume))

    def _before_down(self) -> None:
        """XRay loses its traffic counters: the collector takes a last reading first."""
        if self._name == XRAY_CONTAINER:
            xray_restarting.send_robust(type(self), source=self.instance.source, host=self._host)

    def _wait_until_serving(self) -> None:
        if self._name == XRAY_CONTAINER:
            with self._ctx.step(_("Wait for XRay to start")):
                XrayDocker(self._host, self._ctx).wait_for_xray()
            return
        if self._name == OPENVPN_CONTAINER:
            with self._ctx.step(_("Wait for OpenVPN to start")):
                OpenVpnDocker(self._host, self._ctx).wait_for_openvpn()
            return
        if self._name == IPSEC_CONTAINER:
            # Upstream start.sh is empty: after a restart pluto is started as at the install.
            with self._ctx.step(_("Wait for IPsec to start")):
                IpsecDocker(self._host, self._ctx).ensure_running()
            return
        with self._ctx.step(_("Wait for the VPN interface")):
            AwgContainer(self._host, self._ctx, WG_CONTAINERS[self._name]).wait_for_awg()


class RemovedInstance:
    """The panel's side of a removal: the clients are gone from the server.

    `keep` leaves the instance with its keys and backup, marked removed, so a restore brings the
    clients back; `forget` revokes the keys and deletes the instance with its backup.
    """

    def __init__(self, instance: ProtocolInstance, ctx: OperationContext) -> None:
        self._instance = instance
        self._ctx = ctx

    @transaction.atomic
    def keep(self) -> None:
        instance = self._instance
        self._drop_from_server()
        instance.container_removed_at = timezone.now()
        instance.save(update_fields=["container_removed_at"])
        self._ctx.log(_("The keys and the backup are kept: the protocol can be restored."))

    @transaction.atomic
    def forget(self) -> None:
        instance = self._instance
        keys = AccessKey.objects.filter(source=instance.source).exclude(status=KeyStatus.REVOKED)
        owners = set(User.objects.filter(access_keys__in=keys).distinct())
        revoked = keys.update(status=KeyStatus.REVOKED, revoked_at=timezone.now())
        server = instance.server.name
        for owner in owners:
            transaction.on_commit(partial(keys_revoked.send, type(self), user=owner, server=server))
        self._drop_from_server()
        instance.delete()
        self._ctx.log(_("Keys revoked: %(count)d.") % {"count": revoked})
        self._ctx.log(_("Owners notified: %(count)d.") % {"count": len(owners)})

    def _drop_from_server(self) -> None:
        server = self._instance.server
        name = self._instance.container
        server.record_containers([c for c in server.containers if c["name"] != name])
