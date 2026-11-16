"""Keys of users a snapshot restore made active again: what the plans of their owners have no
place for goes off, and the owners hear that their keys of the server changed."""

from collections import defaultdict

from apps.accounts.models import User
from apps.groups.services.addressing import tell_owners
from apps.keys.models import GONE_STATUSES, AccessKey
from apps.keys.services.limits import disable_over_plan
from apps.plans.services.limits import EffectiveLimits, effective_limits
from apps.protocols.models import ProtocolInstance


class ReturnedKeys:
    """Keys of one instance that came back to their owners."""

    def __init__(self, instance: ProtocolInstance, keys: list[AccessKey]) -> None:
        self._instance = instance
        self._keys = [key for key in keys if key.user_id is not None]

    def over_plan(self) -> list[AccessKey]:
        """Keys on a server or protocol the plan does not allow, and the newest of those over
        the plan's number of keys. Users without a plan keep every key."""
        by_owner: dict[User, list[AccessKey]] = defaultdict(list)
        for key in self._keys:
            if key.user is not None:
                by_owner[key.user].append(key)
        over: list[AccessKey] = []
        for owner, keys in by_owner.items():
            limits = effective_limits(owner)
            if limits.has_plan:
                over += self._owner_over(owner, limits, keys)
        return over

    def settle(self) -> tuple[int, int]:
        """Turn off the keys over the plans and tell the owners; how many went off and how many
        owners heard."""
        started = disable_over_plan(self.over_plan())
        told = tell_owners(self._keys, self._instance.server.name) if self._keys else 0
        return len(started), told

    def _owner_over(
        self, owner: User, limits: EffectiveLimits, keys: list[AccessKey]
    ) -> list[AccessKey]:
        if not self._allowed(limits):
            return keys
        if limits.max_keys is None:
            return []
        live = owner.access_keys.exclude(status__in=GONE_STATUSES).count()
        extra = max(0, live - limits.max_keys)
        newest = sorted(keys, key=lambda key: key.created_at, reverse=True)
        return newest[:extra]

    def _allowed(self, limits: EffectiveLimits) -> bool:
        return limits.allows_protocol(self._instance.container) and limits.allows_server(
            self._instance.server_id
        )
