"""Key actions for the admin interface and the portal. Every server change runs as an operation."""

from django.utils import timezone

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.backends import new_credentials
from apps.keys.services.instances import instances
from apps.keys.services.reissue import KeyReissue, mark_revoking
from apps.operations.models import Operation
from apps.operations.services.launcher import launch

ISSUE = "keys.issue"
DISABLE = "keys.disable"
ENABLE = "keys.enable"
REVOKE = "keys.revoke"
ASSIGN = "keys.assign"
RENAME = "keys.rename"
VIEW = "keys.view"
RESET_VIEW = "keys.reset_view"

# The statuses a key may be in for each state change the admin starts by name.
STATE_CHANGES: dict[str, frozenset[KeyStatus]] = {
    "disable": frozenset({KeyStatus.ACTIVE}),
    "enable": frozenset({KeyStatus.DISABLED_MANUAL, KeyStatus.DISABLED_LIMIT}),
    "revoke": frozenset(
        {KeyStatus.ACTIVE, KeyStatus.DISABLED_MANUAL, KeyStatus.DISABLED_LIMIT, KeyStatus.ISSUING}
    ),
}


class OwnerCannotHaveKeysError(Exception):
    """The user waits for approval or is blocked (DESIGN.md 6.6)."""


class KeyRevokingError(Exception):
    """The key is revoked or being revoked already: re-issued or deleted a moment ago."""


class KeyActions:
    """Every action is written to the audit journal with the actor and their address."""

    def __init__(self, actor: User, ip: str | None = None) -> None:
        self._actor = actor
        self._ip = ip

    def issue(self, source: str, name: str, owner: User | None) -> Operation:
        """Save the key with fresh keys first: a retried operation adds the same peer again."""
        if owner is not None:
            _check_owner(owner)
        public_key, private_key = new_credentials(instances.get(source))
        key = AccessKey.objects.create(
            user=owner,
            source=source,
            name=name,
            public_key=public_key,
            private_key=private_key,
        )
        return self._launch(ISSUE, key)

    def disable(self, key: AccessKey) -> Operation:
        return self._launch(DISABLE, key)

    def enable(self, key: AccessKey) -> Operation:
        return self._launch(ENABLE, key)

    def revoke(self, key: AccessKey) -> Operation:
        """The key becomes `revoking` first: a second press finds it so and starts nothing."""
        if not mark_revoking(key):
            raise KeyRevokingError(key.pk)
        return self._launch(REVOKE, key)

    def change_state(self, key: AccessKey, change: str) -> Operation | None:
        """Disable, enable or revoke by name; None when the key's status does not allow it."""
        if key.status not in STATE_CHANGES.get(change, ()):
            return None
        try:
            operation: Operation = getattr(self, change)(key)
        except KeyRevokingError:
            return None
        return operation

    def reissue(self, key: AccessKey) -> AccessKey:
        """A new key for the same owner, instance and device, by the batch operation of one key.

        The old key is `revoking` at once. Returns the new key.
        """
        started = KeyReissue(self._actor, self._ip).start([key])
        if started.kept:
            raise OwnerCannotHaveKeysError(key.user.get_username() if key.user else "")
        if not started.issued:
            raise KeyRevokingError(key.pk)
        return started.issued[0]

    def rename(self, key: AccessKey, name: str) -> Operation:
        """The new name is also written to clientsTable, where the Amnezia app shows it."""
        return self._launch(RENAME, key, name=name)

    def assign(self, key: AccessKey, owner: User) -> Operation:
        """Give a key to a user; the owner is also written to clientsTable on the server."""
        _check_owner(owner)
        return self._launch(ASSIGN, key, owner_id=owner.pk)

    def record_view(self, key: AccessKey) -> None:
        self._audit(VIEW, key)

    def reset_first_view(self, key: AccessKey) -> None:
        key.first_viewed_at = None
        key.save(update_fields=["first_viewed_at"])
        self._audit(RESET_VIEW, key)

    def _launch(self, kind: str, key: AccessKey, **params: int | str) -> Operation:
        operation = launch(
            kind, {"key_id": key.pk, **params}, user=self._actor, subject=key_subject(key)
        )
        self._audit(kind, key, operation=operation.pk, **params)
        return operation

    def _audit(self, action: str, key: AccessKey, **data: int | str | None) -> None:
        audit.log(
            action,
            actor=self._actor,
            target=key,
            data={"owner_id": key.user_id, "source": key.source, **data},
            ip=self._ip,
        )


def _check_owner(owner: User) -> None:
    if not owner.can_have_keys:
        raise OwnerCannotHaveKeysError(owner.get_username())


def key_subject(key: AccessKey) -> str:
    """How an operation on the key is shown in the journal."""
    owner = key.user.get_username() if key.user else "—"
    return f"{key.name} · {owner} · {timezone.localdate(key.created_at):%Y-%m-%d}"
