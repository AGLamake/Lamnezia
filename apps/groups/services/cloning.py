"""Bringing a server to the identity of its group, and back to a clean standalone one, over SSH.

Both run in a background operation under the server lock (DESIGN.md 5.4): read the files,
keep a snapshot of a server with clients, write the new files with awg0.conf last, restart
the interface, read them back and save what the server now runs with.
"""

from collections.abc import Mapping
from contextlib import ExitStack

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from amnezia_core.awg import AwgServerConfig
from amnezia_core.awg_backup import AwgServerFiles
from amnezia_core.awg_group import AwgGroupIdentity, has_clients, standalone_files
from amnezia_core.containers import AWG2
from amnezia_core.errors import AmneziaFormatError
from apps.backups.models import SnapshotReason
from apps.backups.services.snapshot import read_awg_files, save_backup, take_snapshot
from apps.groups.errors import GroupError, GroupErrorCode
from apps.groups.models import GroupMember, MemberState
from apps.groups.services.membership import take_identity
from apps.operations.services.context import OperationContext
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.awg_container import AwgContainer
from apps.protocols.services.awg_settings import kept_files
from apps.servers.services.connection import connect
from apps.ssh.errors import SshError
from apps.ssh.services import RemoteHost


def read_identity(member: GroupMember, ctx: OperationContext) -> AwgGroupIdentity:
    """The files of the first other member that answers, the leading server first."""
    others = (
        GroupMember.objects.filter(group_id=member.group_id, state=MemberState.MEMBER)
        .exclude(pk=member.pk)
        .select_related("instance__server")
        .order_by("-leading", "priority", "pk")
    )
    for other in others:
        server = other.instance.server
        try:
            with connect(server) as host:
                files = read_awg_files(host)
            if AwgServerConfig.parse(files.config).private_key != other.instance.private_key:
                ctx.log(_("%(server)s runs with other keys: skipped.") % {"server": server})
                continue
            identity = AwgGroupIdentity(files)
        except SshError as error:
            ctx.log(_("%(server)s: %(error)s") % {"server": server, "error": error.message})
            continue
        except AmneziaFormatError:
            ctx.log(_("%(server)s: the server files cannot be read.") % {"server": server})
            continue
        ctx.log(_("The files of %(server)s are copied.") % {"server": server})
        return identity
    raise GroupError(GroupErrorCode.NO_IDENTITY, str(member.group_id))


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

    def _bring_up(self, files: Mapping[str, bytes]) -> bool:
        for path, data in files.items():
            self._docker.shell.write_file_atomic(path, data)
        return self._docker.restart_awg()


class GroupJoin:
    """Makes the server of a joining member a clone of the group.

    Run again after a failure, it finds the files already in place and only saves the result.
    """

    def __init__(self, member: GroupMember, ctx: OperationContext) -> None:
        self._member = member
        self._instance = member.instance
        self._ctx = ctx

    def run(self) -> None:
        with self._ctx.step(_("Read the files of the group")):
            identity = read_identity(self._member, self._ctx)
        if self._instance.port != identity.port:
            raise GroupError(GroupErrorCode.OTHER_PORT, self._instance.source)
        with ExitStack() as stack:
            with self._ctx.step(_("Connect to the server")):
                host = stack.enter_context(connect(self._instance.server))
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(host.lock())
            with self._ctx.step(_("Read the server files")):
                current = read_awg_files(host)
                differs = bool(identity.compare(current))
                if not differs:
                    self._ctx.log(_("The server already has the files of the group."))
            if differs:
                self._clone(host, current, identity)
            with self._ctx.step(_("Save the result in the panel")):
                self._save(host, identity.files)

    def _clone(self, host: RemoteHost, current: AwgServerFiles, identity: AwgGroupIdentity) -> None:
        config = AwgServerConfig.parse(current.config)
        if config.address.network != identity.subnet.network:
            raise GroupError(GroupErrorCode.OTHER_SUBNET, self._instance.source)
        with self._ctx.step(_("Save a snapshot of the server files")):
            if not has_clients(current):
                self._ctx.log(_("The server has no clients: no snapshot is needed."))
            elif config.private_key != self._instance.private_key:
                raise GroupError(GroupErrorCode.OTHER_SERVER_KEY, self._instance.source)
            else:
                take_snapshot(self._instance, current, SnapshotReason.GROUP_JOIN)
        with self._ctx.step(_("Write the files of the group")):
            MemberFiles(host, self._ctx).replace(identity.member_files())
        with self._ctx.step(_("Check that the server is a clone")):
            left = identity.compare(read_awg_files(host))
            if left:
                raise GroupError(
                    GroupErrorCode.NOT_CLONE, ", ".join(sorted({d.kind for d in left}))
                )

    def _save(self, host: RemoteHost, files: AwgServerFiles) -> None:
        with transaction.atomic():
            revoked = take_identity(self._instance, files)
            GroupMember.objects.filter(pk=self._member.pk, state=MemberState.JOINING).update(
                state=MemberState.MEMBER, joined_at=timezone.now()
            )
        if revoked:
            self._ctx.log(_("Keys of the old server key revoked: %(count)d.") % {"count": revoked})
        save_backup(self._instance, host)


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
                    self._ctx.log(_("Keys revoked: %(count)d.") % {"count": revoked})
                save_backup(self._instance, host)
