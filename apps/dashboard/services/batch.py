"""Bulk actions on users (DESIGN.md 6.1): each one is a batch of key operations with a report.

Limits change in the database at once; the keys follow through operations, started right away
instead of waiting for the next run of the limit enforcer. Keys are reissued by one operation per
protocol instance for all the users, so users with keys on one instance share it.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from django.db import transaction
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.dashboard.models import BatchAction, Outcome, UserBatch, UserBatchItem
from apps.keys.models import GONE_STATUSES, KeyStatus
from apps.keys.services.actions import KeyActions
from apps.keys.services.reissue import KeyReissue, ReissueStart
from apps.keys.services.zone import KeyZone
from apps.operations.models import Operation, OperationStatus
from apps.plans.models import Plan, UserLimits
from apps.plans.services.limits import EffectiveLimits, assign_plan
from apps.plans.services.renewal import extend_access
from apps.traffic.services.limits import LimitEnforcer


@dataclass(frozen=True)
class _Result:
    outcome: Outcome
    note: str = ""
    operations: list[Operation] = field(default_factory=list)


class UserBatchActions:
    """Every user gets an outcome in the report and an event in the audit journal.

    A moderator reissues and turns off only the keys of their zone: those on their servers.
    """

    def __init__(
        self, actor: User, ip: str | None = None, clock: Callable[[], datetime] = timezone.now
    ) -> None:
        self._actor = actor
        self._ip = ip
        self._clock = clock
        self._keys = KeyZone(actor).keys().select_related("user")

    def extend(self, users: Iterable[User], days: int) -> UserBatch:
        """Move the end of access `days` later, counting from today if it has already passed."""
        batch = UserBatch(action=BatchAction.EXTEND, days=days)
        return self._run(batch, users, lambda user: self._extend(user, days))

    def disable(self, users: Iterable[User]) -> UserBatch:
        """Turn off every key of the users by hand: no limit change turns them back on."""
        return self._run(UserBatch(action=BatchAction.DISABLE), users, self._disable)

    def change_plan(self, users: Iterable[User], plan: Plan) -> UserBatch:
        """Give the users another plan; the access term starts again."""
        batch = UserBatch(action=BatchAction.CHANGE_PLAN, plan=plan)
        return self._run(batch, users, lambda user: self._change_plan(user, plan))

    def reissue(self, users: Iterable[User]) -> UserBatch:
        """Give every key of the users a new one; the old keys stop working at once."""
        users = list(users)
        with transaction.atomic():
            keys = (
                self._keys.filter(user__in=users).exclude(status__in=GONE_STATUSES).order_by("pk")
            )
            started = KeyReissue(self._actor, self._ip).start(keys)
            batch = UserBatch(action=BatchAction.REISSUE)
            return self._run(batch, users, lambda user: self._reissued(user, started))

    def _run(
        self, batch: UserBatch, users: Iterable[User], act: Callable[[User], _Result]
    ) -> UserBatch:
        with transaction.atomic():
            batch.created_by = self._actor
            batch.save()
            for user in users:
                self._record(batch, user, act(user))
        return batch

    def _record(self, batch: UserBatch, user: User, result: _Result) -> None:
        item = UserBatchItem.objects.create(
            batch=batch, user=user, outcome=result.outcome, note=result.note
        )
        item.operations.set(result.operations)
        audit.log(
            f"users.{batch.action}",
            actor=self._actor,
            target=user,
            data={
                "batch": batch.pk,
                "outcome": result.outcome,
                "days": batch.days,
                "plan_id": batch.plan_id,
            },
            ip=self._ip,
        )

    def _extend(self, user: User, days: int) -> _Result:
        limits = (
            UserLimits.objects.select_for_update(of=("self",))
            .select_related("plan")
            .filter(user=user, plan__isnull=False)
            .first()
        )
        if limits is None:
            return _Result(Outcome.SKIPPED, _("No plan."))
        if extend_access(limits, days, self._clock()) is None:
            return _Result(Outcome.SKIPPED, _("Access has no end date."))
        return self._apply_limits(user, limits)

    def _change_plan(self, user: User, plan: Plan) -> _Result:
        current = UserLimits.objects.filter(user=user).values_list("plan_id", flat=True).first()
        if current == plan.pk:
            return _Result(Outcome.SKIPPED, _("Already on this plan."))
        return self._apply_limits(user, assign_plan(user, plan, self._clock()))

    def _apply_limits(self, user: User, limits: UserLimits) -> _Result:
        """Turn the keys on or off as the new limits say and tell when the access ends."""
        effective = EffectiveLimits.of(limits)
        enforcer = LimitEnforcer(self._clock())
        started = enforcer.switch(user, enforcer.reason(user, effective))
        if effective.access_until is None:
            note = _("Access has no end date.")
        else:
            until = date_format(timezone.localtime(effective.access_until), "SHORT_DATE_FORMAT")
            note = _("Access until %(date)s.") % {"date": until}
        return _Result(Outcome.DONE, note, started)

    def _disable(self, user: User) -> _Result:
        keys = self._keys.filter(user=user, status__in=(KeyStatus.ACTIVE, KeyStatus.DISABLED_LIMIT))
        actions = KeyActions(self._actor, self._ip)
        started = [actions.disable(key) for key in keys if key.status == KeyStatus.ACTIVE]
        # Already off on the server: only the reason changes, so the limits never turn them on.
        keys.filter(status=KeyStatus.DISABLED_LIMIT).update(status=KeyStatus.DISABLED_MANUAL)
        if not started:
            return _Result(Outcome.SKIPPED, _("No active keys."))
        note = _("Keys to turn off: %(count)d.") % {"count": len(started)}
        return _Result(Outcome.DONE, note, started)

    @staticmethod
    def _reissued(user: User, started: ReissueStart) -> _Result:
        if any(key.user_id == user.pk for key in started.kept):
            return _Result(Outcome.SKIPPED, _("The user may not have keys."))
        keys = [key for key in started.reissued if key.user_id == user.pk]
        if not keys:
            return _Result(Outcome.SKIPPED, _("No keys."))
        operations = {started.operation_for(key).pk: started.operation_for(key) for key in keys}
        note = _("Keys to reissue: %(count)d.") % {"count": len(keys)}
        return _Result(Outcome.DONE, note, list(operations.values()))


_UNFINISHED = (OperationStatus.PENDING, OperationStatus.RUNNING)


@dataclass(frozen=True)
class BatchReport:
    """The outcome of a bulk action: users done and skipped, key operations by status."""

    batch: UserBatch
    items: list[UserBatchItem]

    @classmethod
    def of(cls, batch: UserBatch) -> "BatchReport":
        items = batch.items.select_related("user").prefetch_related("operations")
        return cls(batch, list(items))

    @property
    def done(self) -> int:
        return sum(item.outcome == Outcome.DONE for item in self.items)

    @property
    def skipped(self) -> int:
        return len(self.items) - self.done

    @property
    def operations(self) -> list[Operation]:
        """Each operation once: a reissue shares one operation per instance among users."""
        unique = {op.pk: op for item in self.items for op in item.operations.all()}
        return list(unique.values())

    @property
    def succeeded(self) -> int:
        return self._count(OperationStatus.SUCCEEDED)

    @property
    def failed(self) -> int:
        return self._count(OperationStatus.FAILED)

    @property
    def unfinished(self) -> int:
        return self._count(*_UNFINISHED)

    def _count(self, *statuses: OperationStatus) -> int:
        return sum(operation.status in statuses for operation in self.operations)

    @property
    def is_finished(self) -> bool:
        return self.unfinished == 0
