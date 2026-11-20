"""Who answers for a server: its creator joins its staff, the last owner deletes it, others leave.

A server shared by several owners (server administrators) is deleted only by the administrator
or by its one remaining owner; any other owner can only leave its list. Deleting forgets the
server in the panel and touches nothing on it, so a server still running protocols or services
is refused: their keys and backups would be lost.
"""

from functools import cached_property

from django.db import transaction
from django.utils.translation import gettext as _

from apps.accounts.models import Role, User
from apps.accounts.services.zone import Zone
from apps.audit.services import journal as audit
from apps.servers.models import Server
from apps.ssh.services import KnownHostVerifier

DELETE = "servers.delete"
LEAVE = "servers.leave"


class ServerStaffError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ServerStaff:
    def __init__(self, server: Server, member: User) -> None:
        self._server = server
        self._member = member
        self._zone = Zone(member)

    def join(self) -> None:
        """The member who added the server answers for it; the administrator always does."""
        if not self._zone.is_whole:
            self._server.moderators.add(self._member)

    @property
    def may_delete(self) -> bool:
        return self._zone.is_whole or self._owners == [self._member.pk]

    @property
    def may_leave(self) -> bool:
        return not self._zone.is_whole and self._member.pk in self._owners and len(self._owners) > 1

    @transaction.atomic
    def delete(self, ip: str | None = None) -> None:
        self._lock()
        if not self.may_delete:
            raise ServerStaffError(_("The server has other owners: leave its list instead."))
        if self._in_use():
            raise ServerStaffError(_("Remove the protocols and services of the server first."))
        server = self._server
        audit.log(DELETE, actor=self._member, target=server, data={"name": server.name}, ip=ip)
        KnownHostVerifier().forget(server.host, server.port)
        server.delete()

    @transaction.atomic
    def leave(self, ip: str | None = None) -> None:
        self._lock()
        if not self.may_leave:
            raise ServerStaffError(_("You are the only owner of the server."))
        audit.log(LEAVE, actor=self._member, target=self._server, ip=ip)
        self._server.moderators.remove(self._member)

    def _lock(self) -> None:
        """Two owners leaving at once must not both see the other one stay."""
        Server.objects.select_for_update().filter(pk=self._server.pk).first()
        self.__dict__.pop("_owners", None)

    @cached_property
    def _owners(self) -> list[int]:
        owners = self._server.moderators.filter(role=Role.OWNER)
        return sorted(owners.values_list("pk", flat=True))

    def _in_use(self) -> bool:
        server = self._server
        return (
            server.instances.filter(container_removed_at__isnull=True).exists()
            or server.services.exists()
        )
