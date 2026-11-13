import pytest

from amnezia_core.releases import Platform
from apps.guides.services.devices import DetectedDevice, Device

WINDOWS_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
MAC_SAFARI = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.0 Safari/605.1.15"
)
LINUX_FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0"
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
ANDROID_14_FIREFOX = "Mozilla/5.0 (Android 14; Mobile; rv:131.0) Gecko/131.0 Firefox/131.0"
ANDROID_9_SAMSUNG = (
    "Mozilla/5.0 (Linux; Android 9; SM-G960F) AppleWebKit/537.36 (KHTML, like Gecko) "
    "SamsungBrowser/12.0 Chrome/79.0.3945.136 Mobile Safari/537.36"
)
ANDROID_CHROME_REDUCED = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Mobile Safari/537.36"
)


@pytest.mark.parametrize(
    ("user_agent", "device"),
    [
        (WINDOWS_CHROME, Device.WINDOWS),
        (MAC_SAFARI, Device.MACOS),
        (LINUX_FIREFOX, Device.LINUX),
        (IPHONE, Device.IOS),
        (ANDROID_14_FIREFOX, Device.ANDROID),
        ("curl/8.0", None),
        ("", None),
    ],
)
def test_detects_device(user_agent: str, device: Device | None) -> None:
    assert DetectedDevice.from_user_agent(user_agent).device == device


@pytest.mark.parametrize(
    ("user_agent", "version"),
    [(ANDROID_14_FIREFOX, "11+"), (ANDROID_9_SAMSUNG, "9-10"), (ANDROID_CHROME_REDUCED, None)],
)
def test_android_version_matches_apk_names(user_agent: str, version: str | None) -> None:
    assert DetectedDevice.from_user_agent(user_agent).android_version == version


def test_desktop_has_no_android_version() -> None:
    assert DetectedDevice.from_user_agent(WINDOWS_CHROME).android_version is None


def test_ios_has_no_release_platform() -> None:
    assert Device.IOS.release_platform is None
    assert Device.ANDROID.release_platform is Platform.ANDROID
