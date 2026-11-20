"""The zone of a staff member: the users, servers, plans and moderators they answer for.

An administrator's zone is the whole panel. Anyone else's zone is the users they serve
(`User.managers`), the servers given to them or added by them (`User.moderated_servers`) and the
plans they staff (`Plan.staff`); everything else is as good as missing for them. A user served by
several staff members is in the zone of each. An object nobody staffs, such as a server group
spanning servers of several staff members, is the administrator's alone.
"""

from functools import cached_property

from django.db.models import Model, Q, QuerySet

from apps.accounts.models import STAFF_ROLES, Role, User
from apps.groups.models import ServerGroup
from apps.plans.models import Plan
from apps.servers.models import Server


class Zone:
    def __init__(self, member: User) -> None:
        self._member = member

    @property
    def is_whole(self) -> bool:
        return self._member.is_admin

    def users(self) -> QuerySet[User]:
        if self.is_whole:
            return User.objects.all()
        return User.objects.filter(managers=self._member)

    def servers(self) -> QuerySet[Server]:
        if self.is_whole:
            return Server.objects.all()
        return Server.objects.filter(moderators=self._member)

    def plans(self) -> QuerySet[Plan]:
        if self.is_whole:
            return Plan.objects.all()
        return Plan.objects.filter(staff=self._member)

    def offered_plans(self) -> QuerySet[Plan]:
        """Plans the member may give a user: an owner gives their own; a moderator, as in 1.0.0,
        any plan."""
        if self._member.role == Role.OWNER:
            return self.plans()
        return Plan.objects.all()

    def groups(self) -> QuerySet[ServerGroup]:
        """Groups join servers across the panel, so they stay with the whole zone."""
        return ServerGroup.objects.all() if self.is_whole else ServerGroup.objects.none()

    def moderators(self) -> QuerySet[User]:
        """An owner's moderators: those given the owner's servers or staffing the owner's plans.
        The administrator's are all staff members; a moderator has none."""
        if self.is_whole:
            return User.objects.filter(role__in=STAFF_ROLES)
        if self._member.role != Role.OWNER:
            return User.objects.none()
        mine = Q(moderated_servers__in=self.servers()) | Q(staffed_plans__in=self.plans())
        return User.objects.filter(mine, role=Role.MODERATOR).distinct()

    def team(self) -> QuerySet[User]:
        """The member and their moderators: whose operations and actions the member follows."""
        if self.is_whole:
            return User.objects.all()
        return User.objects.filter(Q(pk=self._member.pk) | Q(pk__in=self.moderators()))

    def narrow[M: Model](
        self, items: QuerySet[M], *, user: str | None = None, server: str | None = None
    ) -> QuerySet[M]:
        """`items` within the zone; `user` and `server` are the lookups that lead to their owner
        and server, e.g. ``narrow(conversations, user="user")`` or ``narrow(servers, server="pk")``.
        """
        if self.is_whole:
            return items
        if user is not None:
            items = items.filter(**{f"{user}__in": self.users()})
        if server is not None:
            items = items.filter(**{f"{server}__in": self.servers()})
        return items

    def has_server(self, server_id: int | None) -> bool:
        """Instances that run on no panel server are for administrators only."""
        return self.is_whole or server_id in self._server_ids

    @cached_property
    def _server_ids(self) -> frozenset[int]:
        return frozenset(self.servers().values_list("pk", flat=True))
