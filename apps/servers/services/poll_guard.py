"""One reading of an object at a time: a cache key held while the reading runs.

A reading that hangs on SSH outlives the beat interval, and the next cycle queues a new one. The
new one finds the key and is skipped instead of racing the slow one; its outcome would otherwise
land before or after the slow one's at random. The key expires after `hold`, the longest a reading
can take, so a worker killed in the middle does not keep the object from being read.
"""

from collections.abc import Callable
from typing import TypeVar
from uuid import uuid4

from django.core.cache import BaseCache, cache

from apps.ssh.services.connection import SshTimeouts

T = TypeVar("T")

# A reading connects once and runs a few commands; each is bounded by the command timeout.
_COMMANDS_PER_READING = 3


class PollGuard:
    """Runs a reading of the object named `key` unless another reading of it is under way."""

    def __init__(self, key: str, hold: float, backend: BaseCache = cache) -> None:
        self._key = f"poll:{key}"
        self._hold = hold
        self._cache = backend

    @classmethod
    def over_ssh(cls, key: str) -> "PollGuard":
        """For a reading over one SSH session: held as long as its commands may take."""
        timeouts = SshTimeouts.from_settings()
        return cls(key, timeouts.connect + _COMMANDS_PER_READING * timeouts.command)

    def busy(self) -> bool:
        return self._cache.get(self._key) is not None

    def run(self, reading: Callable[[], T]) -> T | None:
        """The reading's result, or None when another one holds the object."""
        token = uuid4().hex
        if not self._cache.add(self._key, token, timeout=self._hold):
            return None
        try:
            return reading()
        finally:
            # Only our own key: after `hold` it may belong to a later reading.
            if self._cache.get(self._key) == token:
                self._cache.delete(self._key)
