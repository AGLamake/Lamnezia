"""Bringing a server to the identity of its group, and back to a clean standalone one, over SSH.

All run in a background operation under the server lock (DESIGN.md 5.4): read the files,
keep a snapshot of a server with clients, write the new files with awg0.conf last, restart
the interface, read them back and save what the server now runs with. A server joining the
group or catching up with it copies the files of a member that stays locked meanwhile; a server
without AmneziaWG gets it installed straight with those files.
"""

from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from ipaddress import IPv4Network

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from amnezia_core.awg import AwgServerConfig
from amnezia_core.awg_backup import AwgServerFiles
from amnezia_core.awg_group import AwgGroupIdentity, Divergence, has_clients, standalone_files
from amnezia_core.awg_install import AwgInstallSettings
from amnezia_core.containers import AWG2
from amnezia_core.errors import AmneziaFormatError
from apps.backups.models import SnapshotReason
from apps.backups.services.guard import IdentityGuard
from apps.backups.services.snapshot import read_awg_files, save_backup, take_snapshot
from apps.groups.errors import GroupError, GroupErrorCode
from apps.groups.models import GroupMember, LagReason, MemberState, ServerGroup
from apps.groups.services.addressing import tell_owners
from apps.groups.services.membership import add_installed, take_identity
from apps.keys.models import AccessKey
from apps.keys.services.actions import KeyActions
from apps.operations.services.context import OperationContext
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.awg_container import AwgContainer
from apps.protocols.services.awg_installer import AwgInstaller
from apps.protocols.services.awg_settings import AwgReconfiguration, AwgSettings, kept_files
from apps.servers.models import Server
from apps.servers.services.connection import connect
from apps.ssh.errors import SshError
from apps.ssh.services import RemoteHost


@contextmanager
def locked_identity(member: GroupMember, ctx: OperationContext) -> Iterator[AwgGroupIdentity]:
    """The files of the first other up-to-date member that answers, the leading server first.

    That server stays locked until the block ends: no key change lands on it while its files
    are copied, and a change made after the block reaches the copy as a member.
    """
    with locked_source(member, ctx) as (_source, identity):
        yield identity


@contextmanager
def locked_source(
    member: GroupMember, ctx: OperationContext
) -> Iterator[tuple[GroupMember, AwgGroupIdentity]]:
    """As `locked_identity`, with the member whose files they are."""
    with locked_group_source(member.group_id, ctx, exclude=member.pk) as found:
        yield found


@contextmanager
def locked_group_source(
    group_id: int, ctx: OperationContext, *, exclude: int | None = None
) -> Iterator[tuple[GroupMember, AwgGroupIdentity]]:
    """As `locked_source`, for a server that is not a member: any up-to-date member will do."""
    others = (
        GroupMember.objects.filter(group_id=group_id, state=MemberState.MEMBER, lagging_since=None)
        .select_related("instance__server")
        .order_by("-leading", "priority", "pk")
    )
    if exclude is not None:
        others = others.exclude(pk=exclude)
    for other in others:
        with ExitStack() as stack:
            identity = _read_locked(other, stack, ctx)
            if identity is None:
                continue
            ctx.log(_("The files of %(server)s are read.") % {"server": other.instance.server})
            yield other, identity
            return
    raise GroupError(GroupErrorCode.NO_IDENTITY, str(group_id))


def _read_locked(
    member: GroupMember, stack: ExitStack, ctx: OperationContext
) -> AwgGroupIdentity | None:
    """The files of the member's server, locked in `stack`; None when it cannot give them."""
    server = member.instance.server
    try:
        host = stack.enter_context(connect(server))
        stack.enter_context(host.lock())
        files = read_awg_files(host)
        if AwgServerConfig.parse(files.config).private_key != member.instance.private_key:
            ctx.log(_("%(server)s runs with other keys: skipped.") % {"server": server})
            return None
        return AwgGroupIdentity(files)
    except SshError as error:
        ctx.log(_("%(server)s: %(error)s") % {"server": server, "error": error.message})
    except AmneziaFormatError:
        ctx.log(_("%(server)s: the server files cannot be read.") % {"server": server})
    return None


class MemberFiles:
    """The amnezia-awg2 container of one server whose files are replaced as a set."""

    def __init__(self, host: RemoteHost, ctx: OperationContext) -> None:
        self._ctx = ctx
        self._docker = AwgContainer(host, ctx)

    def replace(self, files: Mapping[str, bytes]) -> None:
        """Write the files in order and restart the interface; if it does not come up, the old
        files come back.
        """
        shell = self._docker.shell
        previous = {
            path: shell.read_file(path) for path in kept_files(AWG2) if self._docker.has_file(path)
        }
        if self._bring_up(files):
            self._docker.check_header_protection()
            return
        if self._bring_up(previous):
            self._ctx.log(_("The VPN interface runs with the old files again."))
        raise SetupError(SetupErrorCode.AWG_NOT_RUNNING, AWG2.interface)

    def write_clients_table(self, text: str) -> None:
        """Only the client list differs: it is written alone, the interface keeps running."""
        self._docker.shell.write_file_atomic(AWG2.clients_table_path, text.encode())

    def _bring_up(self, files: Mapping[str, bytes]) -> bool:
        for path, data in files.items():
            self._docker.shell.write_file_atomic(path, data)
        return self._docker.restart_awg()


class _Cloning:
    """Makes the server of a member a clone of the group; subclasses save what that means.

    Run again after a failure, it finds the files already in place and only saves the result.
    """

    def __init__(self, member: GroupMember, ctx: OperationContext) -> None:
        self._member = member
        self._instance = member.instance
        self._ctx = ctx

    def run(self) -> None:
        with ExitStack() as stack:
            with self._ctx.step(_("Read the files of the group")):
                identity = stack.enter_context(locked_identity(self._member, self._ctx))
            if self._instance.port != identity.port:
                raise GroupError(GroupErrorCode.OTHER_PORT, self._instance.source)
            with self._ctx.step(_("Connect to the server")):
                host = stack.enter_context(connect(self._instance.server))
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(host.lock())
            with self._ctx.step(_("Read the server files")):
                current = read_awg_files(host)
                divergences = identity.compare(current)
                if not divergences:
                    self._ctx.log(_("The server already has the files of the group."))
            if divergences:
                self._clone(host, current, identity, divergences)
            with self._ctx.step(_("Save the result in the panel")):
                self._save(host, identity.files)

    def _clone(
        self,
        host: RemoteHost,
        current: AwgServerFiles,
        identity: AwgGroupIdentity,
        divergences: list[Divergence],
    ) -> None:
        config = AwgServerConfig.parse(current.config)
        if config.address.network != identity.subnet.network:
            self._fit_subnet(host, identity)
        self._keep(current, config)
        files = MemberFiles(host, self._ctx)
        if all(divergence.in_clients_table for divergence in divergences):
            with self._ctx.step(_("Write the client list of the group")):
                files.write_clients_table(identity.files.clients_table)
        else:
            with self._ctx.step(_("Write the files of the group")):
                files.replace(identity.member_files())
        with self._ctx.step(_("Check that the server is a clone")):
            left = identity.compare(read_awg_files(host))
            if left:
                raise GroupError(
                    GroupErrorCode.NOT_CLONE, ", ".join(sorted({d.kind for d in left}))
                )

    def _fit_subnet(self, host: RemoteHost, identity: AwgGroupIdentity) -> None:
        """The server has another client subnet than the group; a joining one is refused."""
        raise GroupError(GroupErrorCode.OTHER_SUBNET, self._instance.source)

    def _keep(self, current: AwgServerFiles, config: AwgServerConfig) -> None:
        """Deal with the files the group files are about to replace; nothing by default."""

    def _save(self, host: RemoteHost, files: AwgServerFiles) -> None:
        raise NotImplementedError


class GroupJoin(_Cloning):
    """A joining server becomes a member; owners of keys of its old identity get group keys."""

    def _keep(self, current: AwgServerFiles, config: AwgServerConfig) -> None:
        with self._ctx.step(_("Save a snapshot of the server files")):
            if not has_clients(current):
                self._ctx.log(_("The server has no clients: no snapshot is needed."))
            elif config.private_key != self._instance.private_key:
                raise GroupError(GroupErrorCode.OTHER_SERVER_KEY, self._instance.source)
            else:
                take_snapshot(self._instance, current, SnapshotReason.GROUP_JOIN)

    def _save(self, host: RemoteHost, files: AwgServerFiles) -> None:
        with transaction.atomic():
            revoked = take_identity(self._instance, files)
            GroupMember.objects.filter(pk=self._member.pk, state=MemberState.JOINING).update(
                state=MemberState.MEMBER,
                joined_at=timezone.now(),
                lagging_since=None,
                lagging_reason=LagReason.NONE,
            )
            if revoked:
                self._ctx.log(
                    _("Keys of the old server key revoked: %(count)d.") % {"count": len(revoked)}
                )
                self._give_group_keys(revoked)
        save_backup(self._instance, host)

    def _give_group_keys(self, revoked: list[AccessKey]) -> None:
        """Every user who had a key of the old identity gets a key of the group, same name."""
        leader = self._member.group.leader
        owned = [key for key in revoked if key.user is not None and key.user.can_have_keys]
        if leader is None or not owned:
            return
        if self._ctx.user is None:
            self._ctx.log(_("The operation has no author to issue the group keys."))
            return
        actions = KeyActions(self._ctx.user)
        for key in owned:
            actions.issue(leader.instance.source, key.name, key.user)
        self._ctx.log(_("Group keys issued to users: %(count)d.") % {"count": len(owned)})
        tell_owners(owned, self._instance.server.name)


class MemberCatchUp(_Cloning):
    """A lagging member gets the files of the group and goes back behind the relay.

    It may have missed a new subnet of the group: start.sh takes the subnet when the container
    starts, so the container moves into it first, as a settings change does.
    """

    def _fit_subnet(self, host: RemoteHost, identity: AwgGroupIdentity) -> None:
        settings = AwgSettings(
            identity.port,
            AwgServerConfig.parse(identity.files.config).awg_params,
            self._instance.dns,
            self._instance.container_logs,
            str(identity.subnet.network),
        )
        with self._ctx.step(_("Move the server into the subnet of the group")):
            AwgReconfiguration(host, self._ctx, AWG2).apply_locked(settings)

    def _save(self, host: RemoteHost, files: AwgServerFiles) -> None:
        with transaction.atomic():
            take_identity(self._instance, files)
            GroupMember.objects.filter(pk=self._member.pk).update(
                lagging_since=None, lagging_reason=LagReason.NONE
            )
        save_backup(self._instance, host)


class GroupInstall:
    """Installs AmneziaWG 3.1 on a server without it straight with the files of the group.

    The server never runs a standalone AmneziaWG of its own and has no clients: no snapshot is
    taken. The installer checks the port before it builds anything. A retry keeps a container
    that already has the key of the group; one with another key is refused untouched.
    """

    def __init__(self, group: ServerGroup, server: Server, ctx: OperationContext) -> None:
        self._group = group
        self._server = server
        self._ctx = ctx

    def run(self) -> None:
        with ExitStack() as stack:
            with self._ctx.step(_("Read the files of the group")):
                _source, identity = stack.enter_context(
                    locked_group_source(self._group.pk, self._ctx)
                )
            with self._ctx.step(_("Connect to the server")):
                host = stack.enter_context(connect(self._server))
            self._installer(host, identity).run()
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(host.lock())
            with self._ctx.step(_("Check that the server is a clone")):
                if identity.compare(read_awg_files(host)):
                    # A retry kept the files of an earlier attempt; the group has changed since.
                    self._ctx.log(_("The server has older files of the group: they are replaced."))
                    MemberFiles(host, self._ctx).replace(identity.member_files())
                files = read_awg_files(host)
                left = identity.compare(files)
                if left:
                    raise GroupError(
                        GroupErrorCode.NOT_CLONE, ", ".join(sorted({d.kind for d in left}))
                    )
            with self._ctx.step(_("Save the result in the panel")):
                member = add_installed(self._group, self._server, files)
                save_backup(member.instance, host)

    def _installer(self, host: RemoteHost, identity: AwgGroupIdentity) -> AwgInstaller:
        config = AwgServerConfig.parse(identity.files.config)
        settings = AwgInstallSettings(
            port=identity.port,
            subnet=IPv4Network(identity.subnet.network),
            params=config.awg_params,
        )
        installer = AwgInstaller(host, self._ctx, settings, identity.files)
        installer.guard = IdentityGuard(
            self._ctx,
            path=AWG2.config_path,
            identity=_private_key,
            expected=config.private_key or "",
            refusal=GroupError(GroupErrorCode.AWG_ON_SERVER, self._server.name),
        )
        return installer


def _private_key(config: str) -> str:
    return AwgServerConfig.parse(config).private_key or ""


class GroupLeave:
    """Gives the server of a leaving member a clean AmneziaWG: a new identity without clients."""

    def __init__(self, member: GroupMember, ctx: OperationContext) -> None:
        self._member = member
        self._instance = member.instance
        self._ctx = ctx

    def run(self) -> None:
        with ExitStack() as stack:
            with self._ctx.step(_("Connect to the server")):
                host = stack.enter_context(connect(self._instance.server))
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(host.lock())
            with self._ctx.step(_("Save a snapshot of the server files")):
                current = read_awg_files(host)
                take_snapshot(self._instance, current, SnapshotReason.GROUP_LEAVE)
                config = AwgServerConfig.parse(current.config)
                fresh = standalone_files(config.address, config.listen_port or self._instance.port)
            with self._ctx.step(_("Write a clean AmneziaWG")):
                MemberFiles(host, self._ctx).replace(fresh.by_path())
            with self._ctx.step(_("Save the result in the panel")):
                with transaction.atomic():
                    revoked = take_identity(self._instance, fresh)
                    GroupMember.objects.filter(pk=self._member.pk).delete()
                if revoked:
                    self._ctx.log(_("Keys revoked: %(count)d.") % {"count": len(revoked)})
                save_backup(self._instance, host)
