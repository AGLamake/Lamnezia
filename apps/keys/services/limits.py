"""Turning the keys of a user off and on by the limits of their plan (DESIGN.md 5.5).

The decision is the traffic app's; here the keys are switched through operations, at most
one unfinished operation per key, so overlapping runs never change a key twice.
"""

from django.db import transaction

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services import actions
from apps.keys.services.actions import key_subject
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch

LIMIT_DISABLE = "keys.limit_disable"
LIMIT_ENABLE = "keys.limit_enable"

# Operations that change a key given by `key_id`.
_KEY_OPERATIONS = (
    actions.ISSUE,
    actions.DISABLE,
    actions.ENABLE,
    actions.REVOKE,
    actions.ASSIGN,
    actions.RENAME,
    LIMIT_DISABLE,
    LIMIT_ENABLE,
)
_UNFINISHED = (OperationStatus.PENDING, OperationStatus.RUNNING)


def disable_by_limit(user: User, reason: str) -> list[Operation]:
    """Start turning off the active keys of the user; `reason` names the limit reached."""
    return _switch(user, KeyStatus.ACTIVE, LIMIT_DISABLE, reason=reason)


def enable_by_limit(user: User) -> list[Operation]:
    """Start turning back on the keys the limits turned off; keys turned off by hand stay off."""
    return _switch(user, KeyStatus.DISABLED_LIMIT, LIMIT_ENABLE)


def _switch(user: User, status: KeyStatus, kind: str, **params: str) -> list[Operation]:
    """Keys with an unfinished operation are left for the next run.

    The key rows stay locked until commit, so a concurrent run waits and then sees the
    operations started here.
    """
    with transaction.atomic():
        keys = list(
            AccessKey.objects.select_for_update()
            .select_related("user")
            .filter(user=user, status=status)
        )
        busy = _busy({key.pk for key in keys})
        return [_launch(kind, key, params) for key in keys if key.pk not in busy]


def _launch(kind: str, key: AccessKey, params: dict[str, str]) -> Operation:
    operation = launch(kind, {"key_id": key.pk, **params}, subject=key_subject(key))
    audit.log(
        kind,
        actor=None,
        target=key,
        data={"owner_id": key.user_id, "source": key.source, "operation": operation.pk, **params},
    )
    return operation


def _busy(key_ids: set[int]) -> set[int]:
    if not key_ids:
        return set()
    return set(
        Operation.objects.filter(
            kind__in=_KEY_OPERATIONS, status__in=_UNFINISHED, params__key_id__in=key_ids
        ).values_list("params__key_id", flat=True)
    )
