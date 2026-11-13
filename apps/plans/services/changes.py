"""What follows an admin's change of a plan or of a user's limits: audit and new speed limits."""

from collections.abc import Collection

from django.db import transaction

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.plans.models import UserLimits
from apps.plans.services.limits import assign_plan
from apps.traffic.tasks import apply_speed_limits

# Fields whose change alters the speed limits of keys.
_SPEED_FIELDS = frozenset({"plan", "speed_limit"})


def apply_speed_if_changed(changed: Collection[str]) -> None:
    """Without waiting for the next scheduled run; the worker does the server work."""
    if _SPEED_FIELDS.intersection(changed):
        transaction.on_commit(apply_speed_limits.delay)


def limits_changed(
    limits: UserLimits, changed: Collection[str], actor: User, ip: str | None = None
) -> None:
    """After the limits form saved `limits`: a new plan starts its term again."""
    if "plan" in changed:
        assign_plan(limits.user, limits.plan)
    if changed:
        audit.log(
            "plans.limits",
            actor=actor,
            target=limits.user,
            data={"plan_id": limits.plan_id, "changed": list(changed)},
            ip=ip,
        )
    apply_speed_if_changed(changed)
