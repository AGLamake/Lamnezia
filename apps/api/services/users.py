"""Users through the API: status, plan, effective limits and usage, within the token's zone.

A page of users costs a fixed number of queries: limits are loaded together and the traffic
of all of them is summed by `LimitEnforcer.usages`. No passwords, 2FA or messenger links.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from django.db.models import QuerySet
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services.zone import Zone
from apps.api.errors import NotFoundError
from apps.plans.models import UserLimits
from apps.plans.services.limits import NO_PLAN, PLAN_SERVERS, EffectiveLimits
from apps.portal.services.usage import Usage
from apps.traffic.services.limits import LimitEnforcer


class UserRecords:
    def __init__(self, zone: Zone, now: datetime | None = None) -> None:
        self._zone = zone
        self._now = now or timezone.now()

    def rows(self) -> QuerySet[User]:
        return self._zone.users().order_by("pk")

    def get(self, pk: int) -> User:
        user = self.rows().filter(pk=pk).first()
        if user is None:
            raise NotFoundError
        return user

    def records(self, users: Sequence[User]) -> list[dict[str, Any]]:
        rows = (
            UserLimits.objects.filter(user__in=users)
            .select_related("plan")
            .prefetch_related(*PLAN_SERVERS)
        )
        stored = {row.user_id: row for row in rows}
        limits = {
            user.pk: EffectiveLimits.of(stored[user.pk]) if user.pk in stored else NO_PLAN
            for user in users
        }
        used = LimitEnforcer(self._now).usages(limits)
        return [
            self._record(user, stored.get(user.pk), limits[user.pk], used.get(user.pk, (0, 0)))
            for user in users
        ]

    def _record(
        self,
        user: User,
        stored: UserLimits | None,
        limits: EffectiveLimits,
        used: tuple[int, int],
    ) -> dict[str, Any]:
        usage = Usage.counted(limits, used, self._now)
        plan = stored.plan if stored else None
        return {
            "id": user.pk,
            "username": user.username,
            "email": user.email,
            "role": user.role,
            "status": user.status,
            "is_active": user.is_active,
            "joined_at": user.date_joined,
            "moderator_id": user.manager_id,
            "plan": {"id": plan.pk, "name": plan.name, "is_trial": plan.is_trial} if plan else None,
            "limits": {
                "traffic_bytes": limits.traffic_limit,
                "period_days": limits.period_days,
                "total_traffic_bytes": limits.total_traffic_limit,
                "max_keys": limits.max_keys,
                "speed_mbit": limits.speed_limit,
                "access_until": limits.access_until,
            },
            "usage": {
                "period_bytes": usage.period_used,
                "total_bytes": usage.total_used,
                "resets_at": usage.resets_at,
                "expired": usage.expired,
            },
        }
