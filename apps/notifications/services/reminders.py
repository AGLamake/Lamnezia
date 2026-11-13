"""Telling users ahead that their access term ends (`access_expiring`)."""

from datetime import datetime, timedelta

from django.utils import timezone

from apps.notifications.events import Event
from apps.notifications.services import delivery
from apps.plans.models import UserLimits
from apps.plans.services.limits import EffectiveLimits


def warn_expiring(now: datetime, notice: timedelta) -> None:
    """Once per term: the key holds the end, so an extension that moves it warns again."""
    subscriptions = UserLimits.objects.filter(plan__isnull=False, user__is_active=True)
    for limits in subscriptions.select_related("plan", "user"):
        until = EffectiveLimits.of(limits).access_until
        if until is None or not now < until <= now + notice:
            continue
        user = limits.user
        delivery.emit(
            Event.ACCESS_EXPIRING,
            {"user": user.username, "until": timezone.localtime(until).date().isoformat()},
            key=f"access_expiring:{user.pk}:{until.isoformat()}",
            user=user,
        )
