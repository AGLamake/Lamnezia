"""What a client may do with their own keys, within their plan (DESIGN.md 6.2, 6.5)."""

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.accounts.models import User
from apps.core.models import KeyRevealPolicy
from apps.core.services.site_settings import cached_settings
from apps.keys.models import GONE_STATUSES, AccessKey, KeyStatus
from apps.keys.services import actions
from apps.keys.services.actions import KeyActions, OwnerCannotHaveKeysError
from apps.keys.services.instances import Instance, instances
from apps.keys.services.profile import KeyExport
from apps.keys.services.zone import KeyQuota
from apps.operations.models import Operation, OperationStatus
from apps.plans.services.limits import EffectiveLimits, effective_limits


class KeyLimitReachedError(Exception):
    """The user already has as many keys as the plan allows, the access term is over, or the
    users of their moderator hold as many keys as the moderator's quota allows."""


class InstanceNotAllowedError(Exception):
    """The plan does not allow this server or protocol, or the instance is gone."""


class MyKeys:
    """The keys of one user. Keys revoked or being revoked are gone for the user: they are not
    shown and take no place in the limits."""

    def __init__(self, user: User, ip: str | None = None) -> None:
        self._user = user
        self._actions = KeyActions(user, ip)
        self.limits: EffectiveLimits = effective_limits(user)

    def all(self) -> QuerySet[AccessKey]:
        return self._user.access_keys.exclude(status__in=GONE_STATUSES).order_by("created_at")

    def revoking(self, pk: int) -> bool:
        """The key was re-issued or deleted a moment ago and its revoke is under way."""
        return self._user.access_keys.filter(pk=pk, status=KeyStatus.REVOKING).exists()

    def instances(self) -> list[Instance]:
        """Instances the plan lets the user get a key on; servers under maintenance are not."""
        return [instance for instance in self._plan_instances() if not instance.maintenance]

    def keyed_instances(self, *keys: AccessKey) -> list[Instance]:
        """Instances of the given keys, or of all the user's keys when none is given."""
        sources = {key.source for key in keys or self.all()}
        return [instance for instance in instances if instance.source in sources]

    def can_get_key(self) -> bool:
        return (
            self._user.can_have_keys
            and not self.limits.is_expired(timezone.now())
            and self.limits.allows_another_key(self.all().count())
            and not KeyQuota.of(self._user).reached()
        )

    def issue(self, source: str, name: str) -> AccessKey:
        """Start issuing a key; the key is `issuing` until its operation ends.

        The rows of the user and of their moderator stay locked until commit, so two requests
        at once cannot both pass the key limit or the moderator's quota.
        """
        with transaction.atomic():
            owners = Q(pk=self._user.pk) | Q(pk=self._user.manager_id)
            list(User.objects.select_for_update().filter(owners).order_by("pk"))
            if not self._user.can_have_keys:
                raise OwnerCannotHaveKeysError(self._user.get_username())
            if not self.can_get_key():
                raise KeyLimitReachedError
            self._allowed(source)
            return _key_of(self._actions.issue(source, name, self._user))

    def reissue(self, key: AccessKey) -> AccessKey:
        """A new key in place of the old one, which stops working at once; the count stays.

        A server under maintenance still reissues the keys it has. A second press finds the
        old key `revoking` and raises `KeyRevokingError`.
        """
        self._plan_instance(key.source)
        return self._actions.reissue(key)

    def rename(self, key: AccessKey, name: str) -> None:
        self._actions.rename(key, name)

    def delete(self, key: AccessKey) -> None:
        """A second press finds the key `revoking` and raises `KeyRevokingError`."""
        self._actions.revoke(key)

    @staticmethod
    def issue_failed(key: AccessKey) -> bool:
        """The key is stuck in `issuing`: its latest issue operation has failed."""
        if key.status != KeyStatus.ISSUING:
            return False
        operation = (
            Operation.objects.filter(kind=actions.ISSUE, params__key_id=key.pk)
            .order_by("-pk")
            .first()
        )
        return operation is not None and operation.status == OperationStatus.FAILED

    @property
    def reveals_once(self) -> bool:
        return _policy() == KeyRevealPolicy.ONCE

    def can_reveal(self, key: AccessKey) -> bool:
        """Under the "once" policy a key is shown only until its owner first opens it."""
        if not key.can_export or key.status == KeyStatus.ISSUING:
            return False
        return not self.reveals_once or key.first_viewed_at is None

    def reveal(self, key: AccessKey) -> KeyExport | None:
        """Show the key and remember the first view; None when it may not be shown.

        The first view is set by one conditional update, so of two requests at once under
        the "once" policy only one gets the key.
        """
        if not self.can_reveal(key):
            return None
        first = (
            AccessKey.objects.filter(pk=key.pk, first_viewed_at=None).update(
                first_viewed_at=timezone.now()
            )
            == 1
        )
        if not first and self.reveals_once:
            return None
        self._actions.record_view(key)
        return KeyExport.of(key)

    def _allowed(self, source: str) -> Instance:
        instance = self._plan_instance(source)
        if instance.maintenance:
            raise InstanceNotAllowedError(source)
        return instance

    def _plan_instance(self, source: str) -> Instance:
        for instance in self._plan_instances():
            if instance.source == source:
                return instance
        raise InstanceNotAllowedError(source)

    def _plan_instances(self) -> list[Instance]:
        return [instance for instance in instances if self._allows(instance)]

    def _allows(self, instance: Instance) -> bool:
        if not self.limits.allows_protocol(instance.container.name):
            return False
        if instance.server_id is None:
            return self.limits.server_ids is None
        return self.limits.allows_server(instance.server_id)


def _policy() -> str:
    return cached_settings.get().key_reveal_policy


def _key_of(operation: Operation) -> AccessKey:
    return AccessKey.objects.get(pk=operation.params["key_id"])
