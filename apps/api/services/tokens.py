"""API tokens: issue one (its value is shown once), deactivate it, find the token of a value."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime

from apps.accounts.models import User
from apps.api.models import ApiToken, TokenScope

PREFIX = "lmz_"
# "lmz_" and eight characters of the value: enough to tell tokens apart, useless to guess one.
_SHOWN_LENGTH = len(PREFIX) + 8


class TokenOwnerError(ValueError):
    """Only an administrator or a moderator may own a token: it sees what its owner sees."""


@dataclass(frozen=True, repr=False)
class IssuedToken:
    """A new token and its value, which exists only here: the database keeps its digest."""

    token: ApiToken
    value: str


class ApiTokens:
    """Tokens are issued and deactivated by administrators; the views log both to the audit."""

    def issue(
        self,
        *,
        name: str,
        owner: User,
        by: User,
        scope: str = TokenScope.READ,
        expires_at: datetime | None = None,
    ) -> IssuedToken:
        if not owner.is_staff_member:
            raise TokenOwnerError(owner.get_username())
        value = PREFIX + secrets.token_urlsafe(32)
        token = ApiToken.objects.create(
            name=name,
            owner=owner,
            scope=scope,
            digest=digest(value),
            prefix=value[:_SHOWN_LENGTH],
            expires_at=expires_at,
            created_by=by,
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
