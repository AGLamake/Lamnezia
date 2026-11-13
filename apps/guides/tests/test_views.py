import json
from pathlib import Path

import pytest
from django.test import Client

from amnezia_core.releases import AmneziaRelease, parse_release
from apps.guides.models import FaqArticle
from apps.guides.services.releases import LatestReleaseProvider

# Pages read SiteSettings (the panel name in the layouts).
pytestmark = pytest.mark.django_db

FIXTURE = Path(__file__).parents[3] / "amnezia_core/tests/fixtures/release_5.0.3.0.json"
ANDROID_14 = "Mozilla/5.0 (Android 14; Mobile; rv:131.0) Gecko/131.0 Firefox/131.0"
ANDROID_9 = "Mozilla/5.0 (Linux; Android 9; SM-G960F) AppleWebKit/537.36 Mobile Safari/537.36"
WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"


@pytest.fixture
def release(monkeypatch: pytest.MonkeyPatch) -> AmneziaRelease:
    parsed = parse_release(json.loads(FIXTURE.read_text(encoding="utf-8")))
    monkeypatch.setattr(LatestReleaseProvider, "latest", lambda self: parsed)
    return parsed


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LatestReleaseProvider, "latest", lambda self: None)


def apk_url(release: AmneziaRelease, android_version: str, abi: str) -> str:
    asset = release.android_apk(android_version, abi)
    assert asset is not None
    return asset.url


def test_index_marks_the_visitors_device(client: Client) -> None:
    html = client.get("/guides/", HTTP_USER_AGENT=WINDOWS).content.decode()

    badge = html.index("badge-success")
    assert html.count("badge-success") == 1
    assert html.index('href="/guides/windows/"') < badge < html.index('href="/guides/linux/"')


def test_desktop_page_links_to_the_latest_release(client: Client, release: AmneziaRelease) -> None:
    html = client.get("/guides/windows/").content.decode()

    (windows,) = [asset for asset in release.assets if asset.name.endswith(".exe")]
    assert f'href="{windows.url}"' in html
    assert release.version in html


def test_android_page_uses_version_from_user_agent(client: Client, release: AmneziaRelease) -> None:
    html = client.get("/guides/android/", HTTP_USER_AGENT=ANDROID_9).content.decode()

    assert apk_url(release, "9-10", "arm64-v8a") in html


def test_android_page_follows_the_chosen_apk(client: Client, release: AmneziaRelease) -> None:
    query = {"android": "11+", "abi": "x86_64"}
    html = client.get("/guides/android/", query, HTTP_USER_AGENT=ANDROID_9).content.decode()

    assert apk_url(release, "11+", "x86_64") in html
    assert apk_url(release, "11+", "arm64-v8a") not in html


def test_android_page_defaults_to_modern_build(client: Client, release: AmneziaRelease) -> None:
    html = client.get("/guides/android/", HTTP_USER_AGENT=ANDROID_14).content.decode()

    assert apk_url(release, "11+", "arm64-v8a") in html
    assert "play.google.com" in html


@pytest.mark.usefixtures("offline")
def test_offline_page_points_to_github_releases(client: Client) -> None:
    html = client.get("/guides/macos/").content.decode()

    assert "alert-warning" in html
    assert "github.com/amnezia-vpn/amnezia-client/releases/latest" in html


@pytest.mark.usefixtures("offline")
def test_ios_page_links_to_app_store(client: Client) -> None:
    html = client.get("/guides/ios/").content.decode()

    assert "apps.apple.com" in html
    assert "alert-warning" not in html


def test_unknown_platform_is_404(client: Client) -> None:
    assert client.get("/guides/symbian/").status_code == 404


def test_faq_lists_only_published_articles_in_order(client: Client) -> None:
    FaqArticle.objects.create(slug="slow", title_ru="Медленно", body_ru="-", position=2)
    FaqArticle.objects.create(slug="no-connection", title_ru="Нет связи", body_ru="-", position=1)
    FaqArticle.objects.create(slug="draft", title_ru="Черновик", body_ru="-", is_published=False)

    html = client.get("/guides/faq/").content.decode()

    assert html.index("Нет связи") < html.index("Медленно")
    assert "Черновик" not in html


def test_faq_article_renders_markdown(client: Client) -> None:
    FaqArticle.objects.create(slug="slow", title_ru="Медленно", body_ru="Смените **сервер**")

    html = client.get("/guides/faq/slow/").content.decode()

    assert "<strong>сервер</strong>" in html


def test_unpublished_faq_article_is_404(client: Client) -> None:
    FaqArticle.objects.create(slug="draft", title_ru="Черновик", body_ru="-", is_published=False)

    assert client.get("/guides/faq/draft/").status_code == 404
