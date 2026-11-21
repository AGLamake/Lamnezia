"""Staff members in the panel: `StaffDesk` creates them, gives them rights, servers, plans,
quotas and users.

Everything goes through the actor's zone and rights: an administrator manages every owner and
moderator; a server administrator only their own moderators (`Zone.moderators`), gives them only
their own servers, plans and users, and rights no higher than their own (`Access.grant`). The
target's servers and plans outside the actor's zone are kept as they are.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property

from django.db import transaction
from django.db.models import QuerySet

from apps.accounts.models import Management, Role, User
from apps.accounts.services.access import Access, Level, NotAllowedError, Section
from apps.accounts.services.zone import Zone
from apps.audit.services import journal as audit
from apps.plans.models import Plan
from apps.servers.models import Server


@dataclass(frozen=True)
class StaffChange:
    """What the actor sets for a staff member on their page."""

    user_quota: int | None
    key_quota: int | None
    servers: Iterable[Server]
    plans: Iterable[Plan]
    levels: Mapping[str, Level]


class StaffDesk:
    """What `actor` does with the staff; every change goes to the audit journal."""

    def __init__(self, actor: User, ip: str | None = None) -> None:
        self._actor = actor
        self._ip = ip

    @cached_property
    def zone(self) -> Zone:
        return Zone(self._actor)

    @cached_property
    def access(self) -> Access:
        return Access(self._actor)

    @property
    def can_manage(self) -> bool:
        return self.access.allows(Section.STAFF, Level.MANAGE)

    def members(self) -> QuerySet[User]:
        """Owners and moderators the actor manages; never an administrator or the actor."""
        if not self.can_manage:
            return User.objects.none()
        members = self.zone.moderators().filter(role__in=(Role.OWNER, Role.MODERATOR))
        return members.exclude(pk=self._actor.pk).order_by("username")

    def roles(self) -> list[Role]:
        """Roles the actor may give: an administrator makes owners and moderators."""
        if not self.can_manage:
            return []
        return [Role.OWNER, Role.MODERATOR] if self.zone.is_whole else [Role.MODERATOR]

    def levels(self, target: User, section: str) -> list[Level]:
        """Levels the actor may give `target` in `section`: up to their own and the role's."""
        top = min(self.access.level(section), Access(target).ceiling(section))
        return [level for level in Level if level <= top]

    def create(self, username: str, email: str, password: str, role: str) -> User:
        if role not in self.roles():
            raise NotAllowedError(role)
        member = User(username=username, email=email, role=role, created_by=self._actor)
        member.set_password(password)
        member.save()
        self._log("staff.create", member, role=role)
        return member

    def set_role(self, target: User, role: str) -> None:
        """An administrator turns an owner into a moderator or back."""
        if not self.zone.is_whole or role not in self.roles():
            raise NotAllowedError(role)
        self._check(target)
        if target.role == role:
            return
        target.role = role
        target.save(update_fields=["role"])
        # A right above the ceiling of the new role is cut by `Access.level` on its own.
        self._log("staff.role", target, role=role)

    def update(self, target: User, change: "StaffChange") -> None:
        """Quotas, the servers and plans of the actor's zone, and the rights that changed."""
        self._check(target)
        mine_servers, mine_plans = self.zone.servers(), self.zone.plans()
        with transaction.atomic():
            target.user_quota, target.key_quota = change.user_quota, change.key_quota
            target.save(update_fields=["user_quota", "key_quota"])
            target.moderated_servers.set(
                [
                    *target.moderated_servers.exclude(pk__in=mine_servers),
                    *self._within(change.servers, mine_servers),
                ]
            )
            target.staffed_plans.set(
                [
                    *target.staffed_plans.exclude(pk__in=mine_plans),
                    *self._within(change.plans, mine_plans),
                ]
            )
            target_access = Access(target)
            for section, level in change.levels.items():
                if target_access.level(section) != level:
                    self.access.grant(target, section, level)
        self._log("staff.update", target)

    def candidates_for(self, assigned: QuerySet[User]) -> QuerySet[User]:
        """Members who may still be added to a server, a plan or a user."""
        return self.members().exclude(pk__in=assigned.values("pk"))

    def add_to_server(self, server: Server, member: User) -> None:
        self._check_server(server)
        self._check(member)
        server.moderators.add(member)
        self._log("staff.add", member, server_id=server.pk)

    def remove_from_server(self, server: Server, member: User) -> None:
        self._check_server(server)
        self._check(member)
        server.moderators.remove(member)
        self._log("staff.remove", member, server_id=server.pk)

    def add_to_plan(self, plan: Plan, member: User) -> None:
        """The plan's users get a link to the member (`PlanBinding`)."""
        self._check_plan(plan)
        self._check(member)
        plan.staff.add(member)
        self._log("staff.add", member, plan_id=plan.pk)

    def remove_from_plan(self, plan: Plan, member: User) -> None:
        self._check_plan(plan)
        self._check(member)
        plan.staff.remove(member)
        self._log("staff.remove", member, plan_id=plan.pk)

    def link(self, user: User, member: User) -> bool:
        """Link a user to a member by hand; False when already linked or the quota is full."""
        self._check_user(user)
        self._check(member)
        if user.pk == member.pk or Management.objects.filter(user=user, member=member).exists():
            return False
        if member.is_over_user_quota(user):
            return False
        Management.objects.create(user=user, member=member)
        self._log("staff.add", member, user_id=user.pk)
        return True

    def unlink(self, user: User, member: User) -> None:
        """Only a link made by hand is removed; one from the plan follows the plan."""
        self._check_user(user)
        self._check(member)
        link = Management.objects.filter(user=user, member=member).first()
        if link is None:
            return
        if link.via_plan:
            raise NotAllowedError(member.pk)
        link.delete()
        self._log("staff.remove", member, user_id=user.pk)

    def _check(self, target: User) -> None:
        if not self.members().filter(pk=target.pk).exists():
            raise NotAllowedError(target.pk)

    def _check_server(self, server: Server) -> None:
        if not self.zone.servers().filter(pk=server.pk).exists():
            raise NotAllowedError(server.pk)

    def _check_plan(self, plan: Plan) -> None:
        if not self.zone.plans().filter(pk=plan.pk).exists():
            raise NotAllowedError(plan.pk)

    def _check_user(self, user: User) -> None:
        if not self.zone.users().filter(pk=user.pk).exists():
            raise NotAllowedError(user.pk)

    @staticmethod
    def _within[M: Server | Plan](chosen: Iterable[M], mine: QuerySet[M]) -> list[M]:
        """The chosen items that are in the actor's zone; anything else is ignored."""
        allowed = set(mine.values_list("pk", flat=True))
        return [item for item in chosen if item.pk in allowed]

    def _log(self, action: str, target: User, **data: object) -> None:
        audit.log(action, actor=self._actor, target=target, data=data, ip=self._ip)
