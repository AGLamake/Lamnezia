"""Changes through the API: issue and revoke a key, extend access, change the plan.

Each one goes the way the panel's own does: the database first, then the operations that bring
the servers in line (DESIGN.md 5.4); nothing here talks to a server. The operations started are
kept on the token, so its status endpoint can answer for them.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from apps.accounts.models import User
from apps.accounts.services.zone import Zone
from apps.api.errors import BadParameterError, ConflictError, ForbiddenError
from apps.api.models import ApiToken
from apps.api.services.permissions import Reach
from apps.dashboard.models import Outcome, UserBatch
from apps.dashboard.services.batch import UserBatchActions
from apps.keys.models import AccessKey
from apps.keys.services.actions import (
    STATE_CHANGES,
    KeyActions,
    KeyRevokingError,
    OwnerCannotHaveKeysError,
)
from apps.keys.services.zone import KeyQuotaReachedError, KeyZone
from apps.operations.models import Operation
from apps.plans.models import UserLimits
from apps.plans.services.limits import EffectiveLimits


@dataclass(frozen=True)
class Started:
    """What a change answers with: the object it touched and the operations to follow."""

    operations: list[Operation] = field(default_factory=list)

    def answer(self, **extra: object) -> dict[str, object]:
        return {**extra, "operation_ids": [operation.pk for operation in self.operations]}


class _TokenActions:
    def __init__(self, token: ApiToken, reach: Reach, ip: str | None) -> None:
        self._token = token
        self._owner = token.owner
        self._reach = reach
        self._ip = ip

    def _started(self, operations: Iterable[Operation]) -> Started:
        started = list(operations)
        self._token.operations.add(*started)
        return Started(started)


class TokenKeyActions(_TokenActions):
    """Keys are issued and revoked as from the owner's key page, within the owner's quota."""

    def issue(self, *, source: str, name: str, user_id: int) -> tuple[AccessKey, Started]:
        zone = KeyZone(self._owner)
        if source not in zone.issue_titles():
            raise BadParameterError("source", "an instance of the token's zone open for new keys")
        owner = zone.owners().filter(pk=user_id).first()
        if owner is None:
            raise BadParameterError("user_id", "an active user of the token's zone")
        if not self._reach.has_new_key(owner.pk, source):
            raise ForbiddenError
        try:
            operation = zone.issue(KeyActions(self._owner, self._ip), source, name, owner)
        except KeyQuotaReachedError:
            raise ConflictError("The key quota of the token's owner is reached.") from None
        except OwnerCannotHaveKeysError:
            raise ConflictError("The user may not have keys.") from None
        key = AccessKey.objects.get(pk=operation.params["key_id"])
        return key, self._started([operation])

    def revoke(self, key: AccessKey) -> Started:
        if key.status not in STATE_CHANGES["revoke"]:
            raise ConflictError("The key is revoked or being revoked already.")
        try:
            operation = KeyActions(self._owner, self._ip).revoke(key)
        except KeyRevokingError:
            raise ConflictError("The key is revoked or being revoked already.") from None
        return self._started([operation])


class TokenUserActions(_TokenActions):
    """Access and plans change as by the bulk actions of the users page, for one user."""

    def extend(self, user: User, days: int) -> Started:
        batch = UserBatchActions(self._owner, self._ip).extend([user], days)
        return self._done(batch, "The user has no plan, or the access has no end date.")

    def change_plan(self, user: User, plan_id: int) -> Started:
        plan = Zone(self._owner).offered_plans().filter(pk=plan_id).first()
        if plan is None:
            raise BadParameterError("plan_id", "a plan the token's owner may give")
        batch = UserBatchActions(self._owner, self._ip).change_plan([user], plan)
        return self._done(batch, "The user is on this plan already.")

    def _done(self, batch: UserBatch, skipped: str) -> Started:
        item = batch.items.get()
        if item.outcome != Outcome.DONE:
            raise ConflictError(skipped)
        return self._started(item.operations.all())


def access_until(user: User) -> datetime | None:
    """The end of the user's access after a change, None when it has none."""
    limits = UserLimits.objects.filter(user=user).select_related("plan").first()
    return EffectiveLimits.of(limits).access_until if limits else None
