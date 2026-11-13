import json
from pathlib import Path

import pytest

from amnezia_core.releases import AmneziaRelease, parse_release
from apps.guides.services.devices import Device
from apps.guides.services.platform_guide import ApkChoice, PlatformGuide
from apps.guides.services.releases import StoreLinks

FIXTURE = Path(__file__).parents[3] / "amnezia_core/tests/fixtures/release_5.0.3.0.json"
STORES = StoreLinks(
    app_store="https://apps.example/amnezia", google_play="https://play.example/amnezia"
)


@pytest.fixture
def release() -> AmneziaRelease:
    return parse_release(json.loads(FIXTURE.read_text(encoding="utf-8")))


def names(guide: PlatformGuide) -> list[str]:
    return [asset.name for asset in guide.downloads]


def test_desktop_offers_its_builds(release: AmneziaRelease) -> None:
    guide = PlatformGuide.build(Device.WINDOWS, release, STORES)

    assert names(guide) == ["AmneziaVPN_5.0.3.0_windows_x64.exe"]
    assert guide.store_url == ""
    assert guide.apk is None


def test_android_defaults_to_modern_arm64(release: AmneziaRelease) -> None:
    guide = PlatformGuide.build(Device.ANDROID, release, STORES)

    assert guide.apk == ApkChoice("11+", "arm64-v8a")
    assert names(guide) == ["AmneziaVPN_5.0.3.0_android11+_arm64-v8a.apk"]
    assert guide.store_url == STORES.google_play
    assert guide.android_versions == ("11+", "9-10")
    assert guide.abis == ("arm64-v8a", "armeabi-v7a", "x86", "x86_64")


def test_android_choice_picks_the_apk(release: AmneziaRelease) -> None:
    guide = PlatformGuide.build(Device.ANDROID, release, STORES, ApkChoice("9-10", "armeabi-v7a"))

    assert names(guide) == ["AmneziaVPN_5.0.3.0_android9-10_armeabi-v7a.apk"]


def test_unknown_android_choice_falls_back_to_default(release: AmneziaRelease) -> None:
    guide = PlatformGuide.build(Device.ANDROID, release, STORES, ApkChoice("4.4", "mips"))

    assert guide.apk == ApkChoice("11+", "arm64-v8a")


def test_ios_links_to_app_store_only(release: AmneziaRelease) -> None:
    guide = PlatformGuide.build(Device.IOS, release, STORES)

    assert guide.downloads == ()
    assert guide.release is None
    assert guide.store_url == STORES.app_store


def test_without_release_android_still_has_google_play() -> None:
    guide = PlatformGuide.build(Device.ANDROID, None, STORES)

    assert guide.downloads == ()
    assert guide.store_url == STORES.google_play


def test_without_release_desktop_has_no_downloads() -> None:
    assert PlatformGuide.build(Device.LINUX, None, STORES).downloads == ()
