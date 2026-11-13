"""Effective limits of a user: the plan with the user's overrides applied (DESIGN.md 6.4)."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.plans.models import Plan, UserLimits


@dataclass(frozen=True)
class EffectiveLimits:
    """What a user may do now. `None` in a limit means unlimited.

    `server_ids` and `protocols` are `None` when the plan allows all of them.
    A user without a plan may do nothing: see `NO_PLAN`.
    """

    plan_id: int | None
    traffic_limit: int | None
    period_days: int
    total_traffic_limit: int | None
    max_keys: int | None
    speed_limit: int | None
    access_until: datetime | None
    period_start: datetime | None
    is_trial: bool
    server_ids: frozenset[int] | None
    protocols: frozenset[str] | None

    @classmethod
    def of(cls, limits: UserLimits) -> "EffectiveLimits":
        plan = limits.plan
        if plan is None:
            return NO_PLAN
        server_ids = frozenset(server.pk for server in plan.servers.all())
        return cls(
            plan_id=plan.pk,
            traffic_limit=_override(limits.traffic_limit, plan.traffic_limit),
            period_days=limits.period_days or plan.period_days,
            total_traffic_limit=_override(limits.total_traffic_limit, plan.total_traffic_limit),
            max_keys=_override(limits.max_keys, plan.max_keys),
            speed_limit=_override(limits.speed_limit, plan.speed_limit),
            access_until=limits.access_until or _access_end(plan, limits.plan_assigned_at),
            period_start=limits.plan_assigned_at,
            is_trial=plan.is_trial,
            server_ids=server_ids or None,
            protocols=frozenset(plan.protocols) or None,
        )

    @property
    def has_plan(self) -> bool:
        return self.plan_id is not None

    def is_expired(self, now: datetime) -> bool:
        return self.access_until is not None and now >= self.access_until

    def current_period_start(self, now: datetime) -> datetime | None:
        """Periods follow each other from the day the plan was assigned."""
        if self.period_start is None:
            return None
        length = timedelta(days=self.period_days)
        return self.period_start + (now - self.period_start) // length * length

    def allows_server(self, server_id: int) -> bool:
        return self.server_ids is None or server_id in self.server_ids

    def allows_protocol(self, container: str) -> bool:
        return self.protocols is None or container in self.protocols

    def allows_another_key(self, keys_in_use: int) -> bool:
        return self.max_keys is None or keys_in_use < self.max_keys


NO_PLAN = EffectiveLimits(
    plan_id=None,
    traffic_limit=0,
    period_days=Plan._meta.get_field("period_days").get_default(),
    total_traffic_limit=0,
    max_keys=0,
    speed_limit=None,
    access_until=None,
    period_start=None,
    is_trial=False,
    server_ids=frozenset(),
    protocols=frozenset(),
)


def effective_limits(user: User) -> EffectiveLimits:
    limits = (
        UserLimits.objects.select_related("plan")
        .prefetch_related("plan__servers")
        .filter(user=user)
        .first()
    )
    return EffectiveLimits.of(limits) if limits else NO_PLAN


def assign_plan(user: User, plan: Plan | None, now: datetime | None = None) -> UserLimits:
    """Give the user a plan; the access term starts again. Personal overrides are kept."""
    with transaction.atomic():
        limits, _created = UserLimits.objects.select_for_update().get_or_create(user=user)
        limits.plan = plan
        limits.plan_assigned_at = (now or timezone.now()) if plan else None
        limits.save(update_fields=["plan", "plan_assigned_at"])
    return limits


def _override(personal: int | None, planned: int | None) -> int | None:
    return planned if personal is None else personal


def _access_end(plan: Plan, assigned_at: datetime | None) -> datetime | None:
    if plan.access_days is None or assigned_at is None:
        return None
    return assigned_at + timedelta(days=plan.access_days)
