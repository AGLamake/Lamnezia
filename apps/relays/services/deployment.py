"""Installing, updating and removing the relay of a server, and the periodic sync of relays.

An update gives every group of the relay its service peers on the members first, then writes
the clone peers of the split groups and the members in order to the agent, and only then takes
the uplink peer off the groups that are forwarded again; a removal takes the peers off. A key
or list change of a split group needs only its files: a lighter operation writes just them. The
relay server is locked only for its own container: a member on the same server takes the same
lock for its peers.
"""

from collections.abc import Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from amnezia_core.relay import RelayConfig
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.groups.models import GroupRouting
from apps.groups.services.addressing import GroupKeyAddress
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.services.host_setup import HostSetup
from apps.relays.errors import RelayError, RelayErrorCode
from apps.relays.models import SKIP_DETAIL_LENGTH, PeerRole, Relay, ServicePeer, SyncSkip
from apps.relays.services.clone import peers_digest
from apps.relays.services.config import RelayPlan, members_on_relay
from apps.relays.services.container import RESERVED_PORTS, RelayContainer
from apps.relays.services.service_peers import ServicePeerPlacement
from apps.servers.models import Server
from apps.servers.services.connection import connect
from apps.ssh.services import RemoteHost

INSTALL_KIND = "relays.install"
UPDATE_KIND = "relays.update"
REMOVE_KIND = "relays.remove"
PEERS_KIND = "relays.peers"
# Set by services/routing.py: the change of a group's routing runs an update of its own.
ROUTING_KIND = "relays.routing"
KINDS = (INSTALL_KIND, UPDATE_KIND, REMOVE_KIND, PEERS_KIND, ROUTING_KIND)


class RelayDeployment:
    """One operation on the relay of a server."""

    def __init__(self, relay: Relay, ctx: OperationContext) -> None:
        self._relay = relay
        self._ctx = ctx

    def install(self) -> None:
        """Prepare the server, check the ports, build and start the container, then update it."""
        _refuse(RelayPlan.of(self._relay.server), self._relay.server)
        with ExitStack() as stack:
            host = self._connect(stack)
            setup = HostSetup(host, self._ctx)
            setup.check_os()
            self._place_service_peers()
            plan = RelayPlan.of(self._relay.server)
            config = self._config(plan)
            peers = plan.split_files(config)
            with self._lock(host):
                setup.prepare()
                container = RelayContainer(host, self._ctx)
                with self._ctx.step(_("Check the ports of the groups")):
                    container.check_ports(plan.ports)
                container.install(plan.ports)
                container.apply(plan.ports, config.to_json().encode(), peers)
        self._relay.installed_at = timezone.now()
        self._synced(plan, config, peers)
        self._withdraw_uplinks(plan)

    def update(self) -> None:
        """Write the groups and members of now; new ports are checked before they are published."""
        _refuse(RelayPlan.of(self._relay.server), self._relay.server)
        with ExitStack() as stack:
            host = self._connect(stack)
            self._place_service_peers()
            plan = RelayPlan.of(self._relay.server)
            _refuse(plan, self._relay.server)
            config = self._config(plan)
            peers = plan.split_files(config)
            with self._lock(host):
                container = RelayContainer(host, self._ctx)
                with self._ctx.step(_("Check the ports of the groups")):
                    container.check_ports(plan.ports)
                container.apply(plan.ports, config.to_json().encode(), peers)
        self._synced(plan, config, peers)
        self._withdraw_uplinks(plan)

    def write_split_files(self) -> None:
        """Write the clone peers and lists of the split groups after a key or list change;
        nothing else changes.

        Files of groups the panel forwards now stay: the agent may split them still, and only
        an update that also writes its config takes them away.
        """
        plan = RelayPlan.of(self._relay.server)
        config = self._config(plan)
        peers = plan.split_files(config)
        with ExitStack() as stack:
            host = self._connect(stack)
            with self._lock(host):
                RelayContainer(host, self._ctx).write_split_files(peers)
        self._relay.peers_synced = peers_digest(peers)
        self._relay.save(update_fields=["peers_synced"])

    def remove(self) -> None:
        """Drop the container and the service peers of its groups from their members; the keys
        of the groups go back to their leading servers."""
        with ExitStack() as stack:
            host = self._connect(stack)
            with self._lock(host):
                RelayContainer(host, self._ctx).remove()
        groups = [group for group, _members in RelayPlan.of(self._relay.server).groups]
        for group in groups:
            ServicePeerPlacement(group, self._ctx).withdraw()
        self._relay.delete()
        title = _("Move the keys of the groups back to their leading servers")
        self._move_keys([GroupKeyAddress(group) for group in groups], title)

    def _connect(self, stack: ExitStack) -> RemoteHost:
        with self._ctx.step(_("Connect to the server")):
            return stack.enter_context(connect(self._relay.server))

    @contextmanager
    def _lock(self, host: RemoteHost) -> Iterator[None]:
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(host.lock())
            yield

    def _place_service_peers(self) -> None:
        """Every group gets its probe and a split group its uplink too. A group whose leading
        server does not answer is left for the next sync; a failure on an answering leader
        fails the operation, so the relay keeps the members it has."""
        for group, _members in RelayPlan.of(self._relay.server).groups:
            roles = [PeerRole.PROBE]
            if group.routing == GroupRouting.SPLIT:
                roles.append(PeerRole.UPLINK)
            try:
                ServicePeerPlacement(group, self._ctx).place(roles)
            except RelayError as error:
                if error.code != RelayErrorCode.LEADER_UNREACHABLE:
                    raise
                title = _("Leave the service peer of %(group)s for later") % {"group": group.name}
                with self._ctx.step(title):
                    self._ctx.log(error.message)

    def _withdraw_uplinks(self, plan: RelayPlan) -> None:
        """Take the uplink off the members of the groups forwarded again, now that the agent
        no longer uses it; with the leading server down it stays for the next update."""
        for group, _members in plan.groups:
            forwarded = group.routing == GroupRouting.FORWARD
            uplink = ServicePeer.objects.filter(group=group, role=PeerRole.UPLINK)
            if forwarded and uplink.exists():
                ServicePeerPlacement(group, self._ctx).withdraw([PeerRole.UPLINK])

    def _config(self, plan: RelayPlan) -> RelayConfig:
        with self._ctx.step(_("Prepare the list of members")):
            try:
                config = plan.config()
            except OSError as error:
                raise RelayError(RelayErrorCode.HOST_NOT_RESOLVED, str(error)) from error
            for group in config.groups:
                members = ", ".join(str(backend) for backend in group.backends)
                self._ctx.log(f"{group.port}/udp → {members} ({group.routing})")
            return config

    def _synced(self, plan: RelayPlan, config: RelayConfig, peers: Mapping[str, str]) -> None:
        """Save what the relay runs; the keys of its groups now send clients to it."""
        with self._ctx.step(_("Save the relay state")):
            self._relay.synced = plan.digest(config)
            self._relay.peers_synced = peers_digest(peers)
            self._relay.synced_at = timezone.now()
            self._relay.sync_skipped = SyncSkip.NONE
            self._relay.sync_skipped_detail = ""
            fields = [
                "installed_at",
                "synced",
                "peers_synced",
                "synced_at",
                "sync_skipped",
                "sync_skipped_detail",
            ]
            self._relay.save(update_fields=fields)
            moves = [GroupKeyAddress(group) for group, _members in plan.groups]
        self._move_keys(moves, _("Move the keys of the groups to the relay address"))

    def _move_keys(self, moves: list[GroupKeyAddress], title: str) -> None:
        if not any(move.outdated for move in moves):
            return
        with self._ctx.step(title):
            for move in moves:
                self._ctx.log(
                    _("%(group)s: keys moved: %(count)d.")
                    % {"group": move.group.name, "count": move.apply()}
                )


def _refuse(plan: RelayPlan, server: Server) -> None:
    """Refuse, before the panel connects, what no check on the server could make work:
    a port of the panel's web server, or a member of a group on the relay server itself.
    """
    reserved = sorted(plan.ports & RESERVED_PORTS)
    if reserved:
        raise RelayError(RelayErrorCode.PORT_RESERVED, f"udp {reserved[0]}")
    local = members_on_relay(server)
    if local:
        member = local[0]
        raise RelayError(RelayErrorCode.MEMBER_ON_RELAY, f"{member.group.name}: {member.instance}")


def start(kind: str, server: Server, *, user: User, ip: str | None = None) -> Operation:
    """Launch an operation on the relay of the server; install gives the server the role first."""
    with transaction.atomic():
        if kind == INSTALL_KIND:
            Relay.objects.get_or_create(server=server)
        elif not Relay.objects.filter(server=server).exists():
            raise RelayError(RelayErrorCode.NOT_INSTALLED, str(server))
        operation = launch(kind, {"server_id": server.pk}, user=user, subject=str(server))
        audit.log(kind, actor=user, target=server, data={"operation": operation.pk}, ip=ip)
    return operation


def sync_relays(servers: Iterable[int] | None = None) -> None:
    """Start an update of every installed relay whose groups changed since it was written.

    `servers` limits the sync to the relays on them; without it every relay is compared.
    A relay an update cannot help is skipped and the reason is kept for its card: an update
    started by hand would fail the same way, and a sync must not fail every minute.
    """
    relays = Relay.objects.filter(installed_at__isnull=False).select_related("server")
    if servers is not None:
        relays = relays.filter(server_id__in=servers)
    for relay in relays:
        if not _busy(relay):
            _sync(relay)


def _sync(relay: Relay) -> None:
    local = members_on_relay(relay.server)
    if local:
        member = local[0]
        _skip(relay, SyncSkip.MEMBER_ON_RELAY, f"{member.group.name}: {member.instance}")
        return
    plan = RelayPlan.of(relay.server)
    try:
        config = plan.config()
    except OSError as error:
        _skip(relay, SyncSkip.HOST_NOT_RESOLVED, str(error))
        return
    _skip(relay, SyncSkip.NONE, "")
    if plan.digest(config) != relay.synced:
        kind = UPDATE_KIND
    elif peers_digest(plan.split_files(config)) != relay.peers_synced:
        kind = PEERS_KIND
    else:
        return
    with transaction.atomic():
        # A sync by the minute and one by a change must not start two updates.
        Relay.objects.select_for_update().filter(pk=relay.pk).first()
        if not _busy(relay):
            launch(kind, {"server_id": relay.server_id}, subject=str(relay.server))


def _skip(relay: Relay, reason: SyncSkip, detail: str) -> None:
    """Keep why the last sync skipped the relay; NONE clears it."""
    detail = detail[:SKIP_DETAIL_LENGTH]
    if (relay.sync_skipped, relay.sync_skipped_detail) == (reason, detail):
        return
    relay.sync_skipped, relay.sync_skipped_detail = reason, detail
    relay.save(update_fields=["sync_skipped", "sync_skipped_detail"])


def _busy(relay: Relay) -> bool:
    """An operation on the relay runs or waits: a sync would race with it."""
    return Operation.objects.filter(
        kind__in=KINDS,
        status__in=(OperationStatus.PENDING, OperationStatus.RUNNING),
        params__server_id=relay.server_id,
    ).exists()
