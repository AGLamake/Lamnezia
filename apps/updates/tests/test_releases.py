from typing import Any

import pytest

from apps.guides.models import Release
from apps.guides.services.releases import ReleaseFetchError
from apps.updates.services.releases import PanelReleases
from apps.updates.services.versions import Version

pytestmark = pytest.mark.django_db

REPOSITORY = "AGLamake/Lamnezia"


class FakeClient:
    def __init__(self, answer: dict[str, Any] | None = None) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def fetch_latest(self, repository: str) -> dict[str, Any]:
        self.asked.append(repository)
        if self.answer is None:
            raise ReleaseFetchError(repository)
        return self.answer


def releases(client: FakeClient, *, enabled: bool = True, queued: list[int] | None = None) -> Any:
    return PanelReleases(
        client,  # type: ignore[arg-type]
        repository=REPOSITORY,
        enabled=enabled,
        cache_timeout=60,
        request_check=lambda: (queued if queued is not None else []).append(1),
    )


def test_version_parses_only_release_tags() -> None:
    assert Version.parse("v1.2.3") == Version(1, 2, 3)
    assert Version.parse("1.10.0") > Version.parse("1.9.9")  # type: ignore[operator]
    assert Version(1, 2, 3).tag == "v1.2.3"
    for text in ("v1.2", "v1.2.3-rc.1", "1.1.0.dev1", "latest", ""):
        assert Version.parse(text) is None


def test_refresh_saves_only_the_tag_and_the_text_and_latest_reads_them() -> None:
    client = FakeClient({"tag_name": "v1.1.0", "body": "notes", "html_url": "https://evil"})

    release = releases(client).refresh()

    assert release is not None and release.version == Version(1, 1, 0)
    saved = Release.objects.get(repository=REPOSITORY).payload
    assert saved == {"tag_name": "v1.1.0", "body": "notes"}
    latest = releases(FakeClient()).latest()
    assert latest is not None
    assert latest.notes == "notes"
    assert latest.url == "https://github.com/AGLamake/Lamnezia/releases/tag/v1.1.0"
    assert latest.changelog_url == "https://github.com/AGLamake/Lamnezia/blob/v1.1.0/CHANGELOG.md"


def test_the_release_text_is_markdown_without_raw_html() -> None:
    body = (
        "## Added\n\n- **Fast** start\n\n<script>alert(1)</script>"
        '<img src=x onerror="alert(2)"><a href="javascript:alert(3)">x</a>'
    )
    release = releases(FakeClient({"tag_name": "v1.1.0", "body": body})).refresh()

    assert release is not None
    html = str(release.notes_html)
    assert "<h2>Added</h2>" in html and "<strong>Fast</strong>" in html
    for unsafe in ("<script", "alert(1)", "onerror", "javascript:"):
        assert unsafe not in html


def test_a_long_release_text_is_cut_and_folded() -> None:
    long = releases(FakeClient({"tag_name": "v1.1.0", "body": "x" * 30_000})).refresh()
    short = releases(FakeClient({"tag_name": "v1.1.0", "body": "short"})).refresh()
    none = releases(FakeClient({"tag_name": "v1.1.0", "body": None})).refresh()

    assert long is not None and len(long.notes) == 20_000 and long.notes_are_long
    assert short is not None and not short.notes_are_long
    assert none is not None and none.notes == ""


def test_a_failed_check_keeps_the_saved_release() -> None:
    Release.objects.create(repository=REPOSITORY, payload={"tag_name": "v1.1.0"})

    assert releases(FakeClient()).refresh() is None
    assert releases(FakeClient({"tag_name": "nightly"})).refresh() is None

    assert Release.objects.get(repository=REPOSITORY).payload == {"tag_name": "v1.1.0"}


def test_nothing_saved_queues_one_check() -> None:
    queued: list[int] = []

    assert releases(FakeClient(), queued=queued).latest() is None
    assert releases(FakeClient(), queued=queued).latest() is None

    assert queued == [1]


def test_turned_off_check_asks_nobody() -> None:
    client = FakeClient({"tag_name": "v1.1.0"})
    Release.objects.create(repository=REPOSITORY, payload={"tag_name": "v1.1.0"})
    queued: list[int] = []
    off = releases(client, enabled=False, queued=queued)

    assert off.refresh() is None
    assert off.latest() is None
    off.request_check()

    assert client.asked == []
    assert queued == []
