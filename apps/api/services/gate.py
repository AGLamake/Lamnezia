"""The way into the API: the token from the Authorization header and the call rate."""

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.api.errors import (
    TokenInvalidError,
    TokenMissingError,
    TokenRejectedError,
    TooManyCallsError,
)
from apps.api.models import ApiToken
from apps.api.services.tokens import ApiTokens
from apps.audit.services import journal as audit

_SCHEME = "bearer"
_PATH_LIMIT = 200


class ApiThrottle:
    """At most `limit` calls a minute for one subject: a token, or an address with wrong tokens."""

    WINDOW_SECONDS = 60

    def __init__(self, subject: str, limit: int) -> None:
        self._key = f"api:rate:{subject}"
        self._limit = limit

    def hit(self) -> bool:
        """Count a call; False once the calls of this minute are over the limit."""
        if not self._limit:
            return True
        # add() starts the window only once; incr() is atomic, so parallel calls count right.
        cache.add(self._key, 0, timeout=self.WINDOW_SECONDS)
        try:
            count = cache.incr(self._key)
        except ValueError:  # the window expired between add() and incr()
            cache.set(self._key, 1, timeout=self.WINDOW_SECONDS)
            count = 1
        return count <= self._limit

    def exhausted(self) -> bool:
        """Whether the limit is reached, without counting a call."""
        return bool(self._limit) and cache.get(self._key, 0) >= self._limit


class ApiGate:
    """Lets a call in with a working token while its rate allows; raises an ApiError otherwise.

    Wrong tokens are counted by address, and an address over the limit is refused before the
    database is asked, so guessing tokens costs the panel nothing.
    """

    def __init__(self, tokens: ApiTokens | None = None, limit: int | None = None) -> None:
        self._tokens = tokens or ApiTokens()
        self._limit = settings.API_RATE_LIMIT if limit is None else limit

    def admit(self, authorization: str, ip: str | None) -> ApiToken:
        failures = ApiThrottle(f"ip:{ip or 'unknown'}", self._limit)
        if failures.exhausted():
            raise TooManyCallsError(ApiThrottle.WINDOW_SECONDS)
        try:
            token = self._token(authorization)
        except TokenMissingError:
            failures.hit()
            raise
        if not ApiThrottle(f"token:{token.pk}", self._limit).hit():
            raise TooManyCallsError(ApiThrottle.WINDOW_SECONDS)
        return token

    def _token(self, authorization: str) -> ApiToken:
        scheme, _sep, value = authorization.strip().partition(" ")
        if scheme.lower() != _SCHEME or not value.strip():
            raise TokenMissingError
        token = self._tokens.find(value.strip())
        if token is None:
            raise TokenInvalidError
        if not token.works:
            raise TokenRejectedError(token)
        return token


def record_call(token: ApiToken, *, path: str, status: int, ip: str | None) -> None:
    """Every call with a working token goes to the audit (DESIGN.md 5.7)."""
    ApiToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
    _journal("api.call", token, path=path, status=status, ip=ip)


def record_refusal(token: ApiToken, *, path: str, status: int, ip: str | None) -> None:
    """A call with a disabled or expired token; unknown tokens are not journaled."""
    _journal("api.denied", token, path=path, status=status, ip=ip)


def _journal(action: str, token: ApiToken, *, path: str, status: int, ip: str | None) -> None:
    data = {"token": token.prefix, "path": path[:_PATH_LIMIT], "status": status}
    audit.log(action, actor=token.owner, target=token, data=data, ip=ip)
