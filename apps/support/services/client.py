"""The card of a chat's user beside the thread: plan, access, keys and open requests."""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import QuerySet
from django.utils import timezone

from apps.accounts.models import User
from apps.plans.models import Plan
from apps.plans.services.limits import effective_limits
from apps.support.models import OPEN_STATUSES, Conversation
from apps.support.services.context import KeySeen, keys_seen
from apps.support.services.inbox import Inbox


@dataclass(frozen=True)
class ClientCard:
    """What the staff see of a user now, while answering the chat."""

    user: User
    plan: Plan | None
    access_until: datetime | None
    expired: bool
    keys: list[KeySeen]
    open_requests: QuerySet[Conversation]

    @classmethod
    def of(cls, user: User, inbox: Inbox) -> "ClientCard":
        """Only the requests the staff member may see are listed."""
        limits = effective_limits(user)
        plan = Plan.objects.filter(pk=limits.plan_id).first() if limits.plan_id else None
        requests = inbox.requests().filter(user=user, status__in=OPEN_STATUSES)
        return cls(
            user=user,
            plan=plan,
            access_until=limits.access_until,
            expired=limits.is_expired(timezone.now()),
            keys=keys_seen(user),
            open_requests=requests.order_by("-updated_at"),
        )
