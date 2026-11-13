import json

import pytest
from django.test import Client

from apps.accounts.tests.factories import TOTPDeviceFactory
from apps.core.models import SiteSettings
from apps.core.services.site_settings import cached_settings

pytestmark = pytest.mark.django_db


def test_manifest_names_the_panel_and_its_icons(client: Client) -> None:
    site = SiteSettings.load()
    site.site_name = "Мой VPN"
    site.save()
    cached_settings.invalidate()

    response = client.get("/manifest.webmanifest")
    manifest = json.loads(response.content)

    assert response["Content-Type"] == "application/manifest+json"
    assert manifest["name"] == "Мой VPN"
    assert manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"
    assert {icon["purpose"] for icon in manifest["icons"]} == {"any", "maskable"}
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}


def test_worker_caches_only_the_offline_page_and_its_files(client: Client) -> None:
    response = client.get("/sw.js")
    text = response.content.decode()

    assert response["Content-Type"].startswith("text/javascript")
    assert response["Cache-Control"] == "no-cache"
    assert 'const OFFLINE = "/offline/"' in text
    assert "/static/css/app.css" in text
    assert "lamnezia-" in text


def test_offline_page_offers_to_try_again(client: Client) -> None:
    response = client.get("/offline/")

    assert response.status_code == 200
    assert b'href=""' in response.content


def test_pages_link_the_manifest(client: Client) -> None:
    response = client.get("/accounts/login/")

    assert b'rel="manifest" href="/manifest.webmanifest"' in response.content
    assert b'name="theme-color"' in response.content


@pytest.mark.parametrize("address", ["/manifest.webmanifest", "/sw.js", "/offline/"])
def test_open_before_the_second_factor(client: Client, address: str) -> None:
    """The browser fetches these without following the redirect to the code prompt."""
    client.force_login(TOTPDeviceFactory().user)

    assert client.get(address).status_code == 200
