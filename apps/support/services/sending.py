"""A message sent again with the same token is saved once: the chat retries a send whose answer
was lost, and the first try may have reached the panel.
"""

from types import TracebackType

from django.core.cache import cache

from apps.accounts.models import User

# Long enough for any retry; the page makes a new token for every message.
_TTL_SECONDS = 60 * 60


class SendOnce:
    """`with SendOnce(user, token) as fresh:` — send only when `fresh`.

    The first send with a token claims it; a send that fails gives it back, so a retry goes
    through. An empty token (a page without scripts) is never remembered.
    """

    def __init__(self, user: User, token: str) -> None:
        self._key = f"support:sent:{user.pk}:{token}" if token else ""
        self._claimed = False

    def __enter__(self) -> bool:
        if not self._key:
            return True
        self._claimed = cache.add(self._key, True, _TTL_SECONDS)
        return self._claimed

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None and self._claimed:
            cache.delete(self._key)
