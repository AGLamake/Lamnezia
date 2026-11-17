"""Latest panel release on GitHub: refreshed by beat, read from the cache or the database.

Only the release is asked for: GitHub learns nothing about the installation but the address.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self

from celery import current_app
from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from apps.guides.models import Release
from apps.guides.services.releases import GitHubReleaseClient, ReleaseFetchError
from apps.updates.services.versions import Version

CHECK_TASK = "updates.check_release"
_GITHUB = "https://github.com/{repository}"
# While nothing is saved, a page queues a check at most this often (seconds).
_REQUEST_AGAIN_AFTER = 600


@dataclass(frozen=True)
class PanelRelease:
    """A release of the panel; the links are built here, not taken from the GitHub answer."""

    repository: str
    version: Version

    @property
    def url(self) -> str:
        return f"{_GITHUB.format(repository=self.repository)}/releases/tag/{self.version.tag}"

    @property
    def changelog_url(self) -> str:
        return f"{_GITHUB.format(repository=self.repository)}/blob/{self.version.tag}/CHANGELOG.md"


def queue_check() -> None:
    """Asks the worker for a check; by name, since the task module imports this one."""
    transaction.on_commit(lambda: current_app.send_task(CHECK_TASK))


class PanelReleases:
    """The latest release, unless the check is turned off; only `refresh` asks GitHub."""

    def __init__(
        self,
        client: GitHubReleaseClient,
        *,
        repository: str,
        enabled: bool = True,
        cache_timeout: int,
        request_check: Callable[[], None] = queue_check,
    ) -> None:
        self._client = client
        self._repository = repository
        self.enabled = enabled
        self._cache = cache
        self._cache_timeout = cache_timeout
        self._request_check = request_check

    @classmethod
    def from_settings(cls) -> Self:
        return cls(
            GitHubReleaseClient.from_settings(),
            repository=settings.UPDATE_REPOSITORY,
            enabled=settings.UPDATE_CHECK,
            cache_timeout=settings.UPDATE_CHECK_INTERVAL,
        )

    def latest(self) -> PanelRelease | None:
        """None while the check is off or before the first one succeeds (one is queued then)."""
        if not self.enabled:
            return None
        payload = self._cache.get(self._cache_key)
        if payload is None:
            payload = self._saved()
        if not payload:
            self._request_once()
            return None
        return self._release(payload)

    def refresh(self) -> PanelRelease | None:
        """Asks GitHub and saves the answer; when that fails the saved release stays."""
        if not self.enabled:
            return None
        try:
            answer = self._client.fetch_latest(self._repository)
        except ReleaseFetchError:
            return None
        payload = {"tag_name": answer.get("tag_name")}
        release = self._release(payload)
        if release is None:
            return None
        Release.objects.update_or_create(repository=self._repository, defaults={"payload": payload})
        self._cache.set(self._cache_key, payload, self._cache_timeout)
        return release

    def request_check(self) -> None:
        """Queues a check now, for the button on the update page."""
        if self.enabled:
            self._request_check()

    @property
    def _cache_key(self) -> str:
        return f"updates:release:{self._repository}"

    def _saved(self) -> dict[str, Any]:
        saved = Release.objects.filter(repository=self._repository).first()
        payload = dict(saved.payload) if saved else {}
        if payload:
            self._cache.set(self._cache_key, payload, self._cache_timeout)
        return payload

    def _release(self, payload: dict[str, Any]) -> PanelRelease | None:
        tag = payload.get("tag_name")
        version = Version.parse(tag) if isinstance(tag, str) and tag.startswith("v") else None
        return PanelRelease(self._repository, version) if version else None

    def _request_once(self) -> None:
        if self._cache.add(f"{self._cache_key}:queued", True, _REQUEST_AGAIN_AFTER):
            self._request_check()
