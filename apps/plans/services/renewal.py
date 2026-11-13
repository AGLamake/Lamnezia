"""Renewal of access: moving its end date later and renewing a plan someone paid for."""

from datetime import datetime, timedelta

from django.db import transaction

from apps.accounts.models import User
from apps.plans.models import Plan, UserLimits
from apps.plans.services.limits import EffectiveLimits


def extend_access(limits: UserLimits, days: int, now: datetime) -> datetime | None:
    """Move the end of access `days` later, counting from `now` if it has already passed.

    Returns the new end; access without an end date stays as it is and gives None.
    """
    end = EffectiveLimits.of(limits).access_until
    if end is None:
        return None
    limits.access_until = max(end, now) + timedelta(days=days)
    limits.save(update_fields=["access_until"])
    return limits.access_until


def renew(user: User, plan: Plan, days: int | None, now: datetime) -> UserLimits:
    """Give the user `days` more of `plan`; `days` None is access without an end date.

    The plan the user already has is extended; another plan is assigned and its term and
    traffic periods start `now`. Personal limit overrides stay, a personal end date gives way.
    """
    with transaction.atomic():
        limits, _created = UserLimits.objects.select_for_update().get_or_create(user=user)
        same_plan = limits.plan_id == plan.pk
        if same_plan and days is not None and extend_access(limits, days, now):
            return limits
        if not same_plan:
            limits.plan = plan
            limits.plan_assigned_at = now
        limits.access_until = None if days is None else now + timedelta(days=days)
        limits.save(update_fields=["plan", "plan_assigned_at", "access_until"])
    return limits
