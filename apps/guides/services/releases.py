"""Latest Amnezia app release: refreshed from GitHub in the background, read from the database."""

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self

from celery import current_app
from django.conf import settings
from django.core.cache import BaseCache, cache
from django.db import transaction

from amnezia_core.errors import ReleaseFormatError
from amnezia_core.releases import AmneziaRelease, parse_release
from apps.core.services.site_settings import cached_settings
from apps.guides.models import Release

AMNEZIA_REPOSITORY = "amnezia-vpn/amnezia-client"
_API_URL = "https://api.github.com/repos/{repository}/releases/latest"
REFRESH_TASK = "guides.refresh_release"
# While nothing is saved, a page queues a refresh at most this often (seconds).
_REQUEST_AGAIN_AFTER = 600


class ReleaseFetchError(Exception):
    """GitHub did not return a usable release (network, rate limit, bad JSON)."""


@dataclass(frozen=True)
class StoreLinks:
    app_store: str
    google_play: str

    @classmethod
    def from_settings(cls) -> Self:
        return cls(
            app_store=settings.AMNEZIA_APP_STORE_URL, google_play=settings.AMNEZIA_GOOGLE_PLAY_URL
        )


class GitHubReleaseClient:
    """Reads `releases/latest`; the token raises the API limit from 60 to 5000 requests an hour."""

    def __init__(self, token: str = "", timeout: float = 10) -> None:
        self._token = token
        self._timeout = timeout

    @classmethod
    def from_settings(cls) -> Self:
        token = cached_settings.get().github_token or settings.GITHUB_TOKEN
        return cls(token=token, timeout=settings.GITHUB_TIMEOUT)

    def fetch_latest(self, repository: str) -> dict[str, Any]:
        request = urllib.request.Request(
            _API_URL.format(repository=repository), headers=self._headers()
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ReleaseFetchError(f"{repository}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ReleaseFetchError(f"{repository}: response is not an object")
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Lamnezia",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers


def queue_refresh() -> None:
    """Asks the worker for a refresh; by name, since the task module imports this one."""
    transaction.on_commit(lambda: current_app.send_task(REFRESH_TASK))


class LatestReleaseProvider:
    """Answers from the cache or the database; only `refresh`, run by beat, asks GitHub."""

    def __init__(
        self,
        client: GitHubReleaseClient,
        *,
        repository: str = AMNEZIA_REPOSITORY,
        cache_backend: BaseCache = cache,
        cache_timeout: int,
        request_refresh: Callable[[], None] = queue_refresh,
    ) -> None:
        self._client = client
        self._repository = repository
        self._cache = cache_backend
        self._cache_timeout = cache_timeout
        self._request_refresh = request_refresh

    @classmethod
    def from_settings(cls) -> Self:
        return cls(
            GitHubReleaseClient.from_settings(), cache_timeout=settings.RELEASES_CACHE_TIMEOUT
        )

    def latest(self) -> AmneziaRelease | None:
        """None until the first refresh succeeds; meanwhile one is queued for the worker."""
        payload = self._cache.get(self._cache_key)
        if payload is None:
            payload = self._saved()
        if payload is None:
            self._request_once()
            return None
        return parse_release(payload)

    def refresh(self) -> bool:
        """Asks GitHub and saves the answer; when it fails the saved release stays."""
        try:
            payload = self._client.fetch_latest(self._repository)
            parse_release(payload)
        except (ReleaseFetchError, ReleaseFormatError):
            return False
        Release.objects.update_or_create(repository=self._repository, defaults={"payload": payload})
        self._cache.set(self._cache_key, payload, self._cache_timeout)
        return True

    @property
    def _cache_key(self) -> str:
        return f"guides:release:{self._repository}"

    def _saved(self) -> dict[str, Any] | None:
        saved = Release.objects.filter(repository=self._repository).first()
        if saved is None:
            return None
        payload = dict(saved.payload)
        self._cache.set(self._cache_key, payload, self._cache_timeout)
        return payload

    def _request_once(self) -> None:
        if self._cache.add(f"{self._cache_key}:queued", True, _REQUEST_AGAIN_AFTER):
            self._request_refresh()
