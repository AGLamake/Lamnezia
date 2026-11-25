"""API tokens: issue one (its value is shown once), deactivate it, find the token of a value."""

import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction

from apps.accounts.models import User
from apps.accounts.services.zone import Zone
from apps.api.models import Action, ApiToken, Resource, TokenPermission
from apps.api.services.permissions import check_rule
from apps.keys.models import AccessKey
from apps.keys.services.zone import KeyZone
from apps.servers.models import Server

PREFIX = "lmz_"
# "lmz_" and eight characters of the value: enough to tell tokens apart, useless to guess one.
_SHOWN_LENGTH = len(PREFIX) + 8


class TokenOwnerError(ValueError):
    """Only a staff member may own a token: it sees at most what its owner sees."""


class PermissionZoneError(ValueError):
    """A permission names an object outside the owner's zone."""


@dataclass(frozen=True)
class Permission:
    """A permission to give a new token; at most one of `server`, `user` and `key`."""

    resource: str
    action: str = Action.READ
    server: Server | None = None
    user: User | None = None
    key: AccessKey | None = None

    @property
    def binding(self) -> str | None:
        bound = [name for name in ("server", "user", "key") if getattr(self, name) is not None]
        if len(bound) > 1:
            raise ValueError("one object at most")
        return bound[0] if bound else None


# What the tokens of 1.0 could do: read everything of the owner's zone.
READ_ALL = tuple(Permission(resource) for resource in Resource.values)


@dataclass(frozen=True, repr=False)
class IssuedToken:
    """A new token and its value, which exists only here: the database keeps its digest."""

    token: ApiToken
    value: str


class ApiTokens:
    """Staff members issue their own tokens; the views log issuing and disabling to the audit."""

    def issue(
        self,
        *,
        name: str,
        owner: User,
        by: User,
        permissions: Sequence[Permission] = READ_ALL,
        expires_at: datetime | None = None,
    ) -> IssuedToken:
        """A token with `permissions`, each within the owner's zone as it is now."""
        if not owner.is_staff_member:
            raise TokenOwnerError(owner.get_username())
        for permission in permissions:
            check_rule(permission.resource, permission.action, permission.binding)
            _check_zone(owner, permission)
        value = PREFIX + secrets.token_urlsafe(32)
        with transaction.atomic():
            token = ApiToken.objects.create(
                name=name,
                owner=owner,
                digest=digest(value),
                prefix=value[:_SHOWN_LENGTH],
                expires_at=expires_at,
                created_by=by,
            )
            TokenPermission.objects.bulk_create(
                TokenPermission(
                    token=token,
                    resource=permission.resource,
                    action=permission.action,
                    server=permission.server,
                    user=permission.user,
                    key=permission.key,
                )
                for permission in permissions
            )
        return IssuedToken(token, value)

    def deactivate(self, token: ApiToken) -> bool:
        """False when the token was already off. There is no way back: issue a new one."""
        changed = ApiToken.objects.filter(pk=token.pk, is_active=True).update(is_active=False)
        token.is_active = False
        return bool(changed)

    def find(self, value: str) -> ApiToken | None:
        """The token with this value, working or not; None for an unknown value."""
        if not value.startswith(PREFIX):
            return None
        return ApiToken.objects.select_related("owner").filter(digest=digest(value)).first()


def digest(value: str) -> str:
    """The value is random and long, so a plain SHA-256 is enough and lets the database find it."""
    return hashlib.sha256(value.encode()).hexdigest()


def _check_zone(owner: User, permission: Permission) -> None:
    if permission.server is not None:
        inside = Zone(owner).servers().filter(pk=permission.server.pk).exists()
    elif permission.user is not None:
        inside = Zone(owner).users().filter(pk=permission.user.pk).exists()
    elif permission.key is not None:
        inside = KeyZone(owner).keys().filter(pk=permission.key.pk).exists()
    else:
        return
    if not inside:
        raise PermissionZoneError(permission.binding)
