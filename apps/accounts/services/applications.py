"""The queue of new users waiting for a plan in the `open_manual` mode (DESIGN.md 6.6)."""

from django.db import transaction
from django.db.models import QuerySet

from apps.accounts.models import Status, User
from apps.audit.services import journal as audit
from apps.plans.models import Plan
from apps.plans.services.limits import assign_plan


class Applications:
    """Approving gives a plan and opens access; rejecting blocks the account."""

    def __init__(self, actor: User, ip: str | None = None) -> None:
        self._actor = actor
        self._ip = ip

    @staticmethod
    def waiting() -> QuerySet[User]:
        """Users who confirmed their email, if asked to, and wait for an administrator."""
        return User.objects.filter(status=Status.PENDING, is_active=True).order_by("date_joined")

    def approve(self, user: User, plan: Plan) -> None:
        with transaction.atomic():
            assign_plan(user, plan)
            user.status = Status.ACTIVE
            user.save(update_fields=["status"])
            self._audit("accounts.approve", user, plan_id=plan.pk)

    def reject(self, user: User) -> None:
        user.status = Status.BLOCKED
        user.is_active = False
        user.save(update_fields=["status", "is_active"])
        self._audit("accounts.reject", user)

    def _audit(self, action: str, user: User, **data: int) -> None:
        audit.log(action, actor=self._actor, target=user, data=data, ip=self._ip)
