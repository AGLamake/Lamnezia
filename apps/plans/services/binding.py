"""Links between users and the staff of their plan: `PlanBinding.apply` and `.resync`."""

from django.db import transaction

from apps.accounts.models import Management, Role, User
from apps.audit.services import journal as audit
from apps.plans.models import Plan, UserLimits

# Staff roles that can serve users; anyone else on a plan's list gets no link.
_SERVING_ROLES = (Role.OWNER, Role.MODERATOR)


class PlanBinding:
    """A user with a plan is served by its staff; such links are marked `via_plan`.

    Links made by hand are never changed: a member linked by hand stays so, without a duplicate.
    A moderator with no room left in their user quota is skipped, and the journal says so.
    """

    @classmethod
    def apply(cls, user: User, plan: Plan | None) -> None:
        """Make the user's links from the plan match the staff of `plan`; None drops them all."""
        staff = list(plan.staff.filter(role__in=_SERVING_ROLES).exclude(pk=user.pk)) if plan else []
        with transaction.atomic():
            Management.objects.filter(user=user, via_plan=True).exclude(
                member__in=[member.pk for member in staff]
            ).delete()
            linked = set(Management.objects.filter(user=user).values_list("member_id", flat=True))
            for member in staff:
                if member.pk not in linked:
                    cls._link(user, member, plan)

    @classmethod
    def resync(cls, plan: Plan) -> None:
        """After the staff of `plan` changed: the links of every user on it follow."""
        for limits in UserLimits.objects.filter(plan=plan).select_related("user"):
            cls.apply(limits.user, plan)

    @staticmethod
    def _link(user: User, member: User, plan: Plan | None) -> None:
        if member.is_over_user_quota(user):
            audit.log(
                "plans.binding_skipped",
                actor=None,
                target=user,
                data={"member_id": member.pk, "plan_id": plan.pk if plan else None},
            )
            return
        Management.objects.create(user=user, member=member, via_plan=True)
