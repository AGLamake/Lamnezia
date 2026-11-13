import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from django.core.cache.backends.locmem import LocMemCache

from apps.guides.models import Release
from apps.guides.services.releases import (
    AMNEZIA_REPOSITORY,
    GitHubReleaseClient,
    LatestReleaseProvider,
    ReleaseFetchError,
    StoreLinks,
)
from apps.guides.tasks import refresh_release

FIXTURE = Path(__file__).parents[3] / "amnezia_core/tests/fixtures/release_5.0.3.0.json"


def release_payload() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


class FakeGitHub(GitHubReleaseClient):
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.payload = payload
        self.requests = 0

    def fetch_latest(self, repository: str) -> dict[str, Any]:
        self.requests += 1
        if self.payload is None:
            raise ReleaseFetchError("offline")
        return self.payload


@pytest.fixture
def cache() -> LocMemCache:
    backend = LocMemCache("releases-test", {})
    backend.clear()
    return backend


class Queue:
    def __init__(self) -> None:
        self.requests = 0

    def __call__(self) -> None:
        self.requests += 1


def provider_for(
    github: FakeGitHub, cache: LocMemCache, queue: Queue | None = None
) -> LatestReleaseProvider:
    return LatestReleaseProvider(
        github, cache_backend=cache, cache_timeout=60, request_refresh=queue or Queue()
    )


@pytest.mark.django_db
def test_page_never_asks_github(cache: LocMemCache) -> None:
    github = FakeGitHub(release_payload())
    Release.objects.create(repository=AMNEZIA_REPOSITORY, payload=release_payload())
    provider = provider_for(github, cache)

    first = provider.latest()
    cache.clear()
    second = provider.latest()

    assert first is not None
    assert first == second
    assert first.version == "5.0.3.0"
    assert github.requests == 0


@pytest.mark.django_db
def test_refresh_saves_the_release(cache: LocMemCache) -> None:
    provider = provider_for(FakeGitHub(release_payload()), cache)

    assert provider.refresh()

    assert Release.objects.get(repository=AMNEZIA_REPOSITORY).payload["tag_name"] == "5.0.3.0"
    release = provider.latest()
    assert release is not None
    assert release.version == "5.0.3.0"


@pytest.mark.django_db
def test_failed_refresh_keeps_the_saved_release(cache: LocMemCache) -> None:
    Release.objects.create(repository=AMNEZIA_REPOSITORY, payload=release_payload())
    provider = provider_for(FakeGitHub(payload=None), cache)

    assert not provider.refresh()

    release = provider.latest()
    assert release is not None
    assert release.version == "5.0.3.0"


@pytest.mark.django_db
def test_broken_github_answer_is_not_saved(cache: LocMemCache) -> None:
    Release.objects.create(repository=AMNEZIA_REPOSITORY, payload=release_payload())

    assert not provider_for(FakeGitHub({"message": "API rate limit exceeded"}), cache).refresh()

    assert Release.objects.get().payload["tag_name"] == "5.0.3.0"


@pytest.mark.django_db
def test_nothing_saved_queues_one_refresh(cache: LocMemCache) -> None:
    github = FakeGitHub(release_payload())
    queue = Queue()
    provider = provider_for(github, cache, queue)

    assert provider.latest() is None
    assert provider.latest() is None

    assert queue.requests == 1
    assert github.requests == 0


@pytest.mark.django_db
def test_beat_task_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    github = FakeGitHub(release_payload())
    monkeypatch.setattr(GitHubReleaseClient, "from_settings", classmethod(lambda cls: github))

    refresh_release()

    assert github.requests == 1
    assert Release.objects.get().payload["tag_name"] == "5.0.3.0"


def test_client_sends_token_and_reads_json(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[urllib.request.Request] = []

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, *args: object) -> bytes:
            return FIXTURE.read_bytes()

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> Response:
        sent.append(request)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    payload = GitHubReleaseClient(token="ghp_test").fetch_latest(AMNEZIA_REPOSITORY)

    assert payload["tag_name"] == "5.0.3.0"
    assert sent[0].full_url.endswith("/repos/amnezia-vpn/amnezia-client/releases/latest")
    assert sent[0].get_header("Authorization") == "Bearer ghp_test"


def test_client_without_token_sends_no_authorization() -> None:
    assert "Authorization" not in GitHubReleaseClient()._headers()


def test_network_error_becomes_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(request: urllib.request.Request, timeout: float) -> None:
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", offline)

    with pytest.raises(ReleaseFetchError):
        GitHubReleaseClient().fetch_latest(AMNEZIA_REPOSITORY)


def test_store_links_come_from_settings(settings: Any) -> None:
    settings.AMNEZIA_APP_STORE_URL = "https://apps.apple.com/app/id1"

    links = StoreLinks.from_settings()

    assert links.app_store == "https://apps.apple.com/app/id1"
    assert links.google_play.startswith("https://play.google.com/")
