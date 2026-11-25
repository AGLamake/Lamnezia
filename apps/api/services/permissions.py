"""Permissions of API tokens: which resource takes which action and object, and what a call may.

A token reaches what its owner's zone holds at the moment of the call, narrowed by its
permissions; the zone is applied by the record services, the permissions here.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from django.db.models import Q, QuerySet

from apps.accounts.models import User
from apps.accounts.services.access import Access, Level
from apps.api.errors import ForbiddenError
from apps.api.models import Action, ApiToken, Resource, TokenPermission
from apps.keys.models import AccessKey
from apps.protocols.models import ProtocolInstance
from apps.servers.models import Server

SERVER, USER, KEY = "server", "user", "key"


@dataclass(frozen=True)
class Rule:
    """The actions a resource takes and the objects a permission on it may be bound to."""

    actions: frozenset[str]
    bindings: frozenset[str]
    # The panel section whose level the owner needs to manage the resource.
    section: str = ""


_READ = frozenset({Action.READ.value})
_BOTH = frozenset({Action.READ.value, Action.MANAGE.value})

RULES: dict[str, Rule] = {
    Resource.SERVERS: Rule(_READ, frozenset({SERVER})),
    Resource.USERS: Rule(_BOTH, frozenset({USER}), section="users"),
    Resource.KEYS: Rule(_BOTH, frozenset({SERVER, USER, KEY}), section="keys"),
    Resource.TRAFFIC: Rule(_READ, frozenset({SERVER, USER, KEY})),
    Resource.OPERATIONS: Rule(_READ, frozenset()),
}


class PermissionRuleError(ValueError):
    """The action or the object does not go with the resource."""


def check_rule(resource: str, action: str, binding: str | None) -> None:
    rule = RULES[resource]
    if action not in rule.actions:
        raise PermissionRuleError("action")
    if binding is not None and binding not in rule.bindings:
        raise PermissionRuleError("binding")


def binding_of(permission: TokenPermission) -> str | None:
    for name in (SERVER, USER, KEY):
        if getattr(permission, f"{name}_id") is not None:
            return name
    return None


def server_of(source: str) -> int | None:
    """The panel server of the instance a key's `source` names."""
    pk = ProtocolInstance.pk_of(source)
    if pk is None:
        return None
    return ProtocolInstance.objects.filter(pk=pk).values_list("server_id", flat=True).first()


@dataclass(frozen=True)
class Reach:
    """The objects of one resource and action: all of the zone, or those bound by permissions."""

    whole: bool
    servers: frozenset[int] = frozenset()
    users: frozenset[int] = frozenset()
    keys: frozenset[int] = frozenset()

    @classmethod
    def of(cls, permissions: Iterable[TokenPermission]) -> "Reach":
        rows = list(permissions)
        return cls(
            whole=any(binding_of(row) is None for row in rows),
            servers=frozenset(row.server_id for row in rows if row.server_id),
            users=frozenset(row.user_id for row in rows if row.user_id),
            keys=frozenset(row.key_id for row in rows if row.key_id),
        )

    def servers_in(self, servers: QuerySet[Server]) -> QuerySet[Server]:
        return servers if self.whole else servers.filter(pk__in=self.servers)

    def users_in(self, users: QuerySet[User]) -> QuerySet[User]:
        return users if self.whole else users.filter(pk__in=self.users)

    def keys_in(self, keys: QuerySet[AccessKey]) -> QuerySet[AccessKey]:
        if self.whole:
            return keys
        instances = ProtocolInstance.objects.filter(server_id__in=self.servers)
        sources = [ProtocolInstance.source_for(pk) for pk in instances.values_list("pk", flat=True)]
        return keys.filter(Q(pk__in=self.keys) | Q(user_id__in=self.users) | Q(source__in=sources))

    def has_server(self, server_id: int) -> bool:
        return self.whole or server_id in self.servers

    def has_user(self, user_id: int) -> bool:
        return self.whole or user_id in self.users

    def has_key(self, key: AccessKey) -> bool:
        return self.whole or key.pk in self.keys or self.has_new_key(key.user_id, key.source)

    def has_new_key(self, user_id: int | None, source: str) -> bool:
        """A key not issued yet: bound to its owner or server, not to a key."""
        if self.whole or (user_id is not None and user_id in self.users):
            return True
        server = server_of(source) if self.servers else None
        return server is not None and server in self.servers


class TokenGrants:
    """The permissions of a token, read once for a call."""

    def __init__(self, token: ApiToken) -> None:
        self._owner = token.owner
        self._permissions = list(token.permissions.all())

    def reach(self, resource: str, action: str = Action.READ) -> Reach:
        """The objects the call may touch; 403 when no permission gives the action.

        Managing also needs the owner to manage the resource's section of the panel now.
        """
        actions = {Action.MANAGE} if action == Action.MANAGE else {Action.READ, Action.MANAGE}
        rows = [
            row for row in self._permissions if row.resource == resource and row.action in actions
        ]
        if not rows:
            raise ForbiddenError
        section = RULES[resource].section
        if action == Action.MANAGE and not Access(self._owner).allows(section, Level.MANAGE):
            raise ForbiddenError("The token's owner may not manage this in the panel.")
        return Reach.of(rows)

    def described(self) -> list[dict[str, str | int | None]]:
        return [
            {
                "resource": row.resource,
                "action": row.action,
                "server_id": row.server_id,
                "user_id": row.user_id,
                "key_id": row.key_id,
            }
            for row in self._permissions
        ]
