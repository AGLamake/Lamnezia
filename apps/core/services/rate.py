"""Rate limits in the cache: a fixed window that counts hits for one key."""

from django.core.cache import cache


class RateWindow:
    """At most `limit` hits for one key over `seconds` (0: no limit).

    The window starts with the first hit and ends `seconds` later; the count lives in the cache,
    so every worker sees the same one.
    """

    def __init__(self, key: str, limit: int, seconds: int) -> None:
        self._key = key
        self._limit = limit
        self._seconds = seconds

    def hit(self) -> bool:
        """Count a hit; False once the hits of this window are over the limit."""
        if not self._limit:
            return True
        # add() starts the window only once; incr() is atomic, so parallel hits count right.
        cache.add(self._key, 0, timeout=self._seconds)
        try:
            count = cache.incr(self._key)
        except ValueError:  # the window expired between add() and incr()
            cache.set(self._key, 1, timeout=self._seconds)
            count = 1
        return count <= self._limit

    def exhausted(self) -> bool:
        """Whether the limit is reached, without counting a hit."""
        return bool(self._limit) and cache.get(self._key, 0) >= self._limit
