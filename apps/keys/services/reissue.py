"""Keys reissued in a batch: one operation per protocol instance instead of two per key.

The new keys are saved with fresh credentials before the operation starts, so a retried
operation adds the same clients again. The old keys become `revoking` at once: they leave the
limits and cannot be revoked twice. The operation then issues the new keys and revokes the old
ones in one change of the server files (DESIGN.md 5.4).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from django.db import transaction

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.keys.models import GONE_STATUSES, AccessKey, KeyStatus
from apps.keys.services.backends import Reissue, new_credentials
from apps.keys.services.instances import instances
from apps.operations.models import Operation
from apps.operations.services.launcher import launch

REISSUE = "keys.reissue"


@dataclass
class ReissueStart:
    """`reissued`: old keys that get a new one, `issued` in the same order; `kept`: their owner
    may not have keys. A key revoked or being revoked already is in neither list."""

    reissued: list[AccessKey] = field(default_factory=list)
    issued: list[AccessKey] = field(default_factory=list)
    kept: list[AccessKey] = field(default_factory=list)
    operations: list[Operation] = field(default_factory=list)

    def operation_for(self, key: AccessKey) -> Operation:
        """The operation that reissues the key: one per instance."""
        return next(op for op in self.operations if op.params["source"] == key.source)


class KeyReissue:
    """Every reissued key is written to the audit journal with the actor and their address."""

    def __init__(self, actor: User, ip: str | None = None) -> None:
        self._actor = actor
        self._ip = ip

    @transaction.atomic
    def start(self, keys: Iterable[AccessKey]) -> ReissueStart:
        result = ReissueStart()
        batches: dict[str, list[tuple[AccessKey, AccessKey, KeyStatus]]] = {}
        for key in keys:
            if key.user is not None and not key.user.can_have_keys:
                result.kept.append(key)
                continue
            previous = KeyStatus(key.status)
            if not mark_revoking(key):
                continue
            new = _new_key(key)
            batches.setdefault(key.source, []).append((key, new, previous))
            result.reissued.append(key)
            result.issued.append(new)
        result.operations = [self._launch(source, batch) for source, batch in batches.items()]
        return result

    def _launch(
        self, source: str, batch: list[tuple[AccessKey, AccessKey, KeyStatus]]
    ) -> Operation:
        """Each pair keeps the old key's status, given back if the old key stays in force."""
        pairs = [[old.pk, new.pk, previous] for old, new, previous in batch]
        params = {"source": source, "pairs": pairs}
        operation = launch(REISSUE, params, user=self._actor, subject=instances.get(source).title)
        for old, new, _previous in batch:
            audit.log(
                REISSUE,
                actor=self._actor,
                target=old,
                data={
                    "owner_id": old.user_id,
                    "source": source,
                    "new_key_id": new.pk,
                    "operation": operation.pk,
                },
                ip=self._ip,
            )
        return operation


def mark_revoking(key: AccessKey) -> bool:
    """Start the revoke of the key; False when it is revoked or being revoked already.

    One conditional update, so of two requests at once only one starts the revoke.
    """
    started = (
        AccessKey.objects.filter(pk=key.pk)
        .exclude(status__in=GONE_STATUSES)
        .update(status=KeyStatus.REVOKING)
    )
    if started:
        key.status = KeyStatus.REVOKING
    return started == 1


def _new_key(old: AccessKey) -> AccessKey:
    public_key, private_key = new_credentials(instances.get(old.source))
    return AccessKey.objects.create(
        user=old.user,
        source=old.source,
        name=old.name,
        public_key=public_key,
        private_key=private_key,
    )


def pending(pairs: Iterable[Sequence[int | str]]) -> list[Reissue]:
    """The pairs of the operation still to do; a new key revoked meanwhile is not issued."""
    ids = [(int(pair[0]), int(pair[1])) for pair in pairs]
    keys = AccessKey.objects.in_bulk([key_id for pair in ids for key_id in pair])
    return [
        Reissue(keys[old], keys[new])
        for old, new in ids
        if old in keys and new in keys and keys[new].status == KeyStatus.ISSUING
    ]


def give_back(pairs: Iterable[Sequence[int | str]], done: Iterable[AccessKey]) -> None:
    """Old keys not reissued stay in force on the server, so they get their status back."""
    replaced = {key.pk for key in done}
    for old, _new, *previous in pairs:
        if previous and old not in replaced:
            AccessKey.objects.filter(pk=old, status=KeyStatus.REVOKING).update(status=previous[0])
