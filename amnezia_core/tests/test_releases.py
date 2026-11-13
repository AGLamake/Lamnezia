import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from amnezia_core.errors import ReleaseFormatError
from amnezia_core.releases import Platform, parse_release

DOWNLOAD = "https://github.com/amnezia-vpn/amnezia-client/releases/download/5.0.3.0/"


@pytest.fixture
def payload(read_fixture: Callable[[str], str]) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(read_fixture("release_5.0.3.0.json"))
    return data


def test_parses_release_metadata(payload: dict[str, Any]) -> None:
    release = parse_release(payload)

    assert release.version == "5.0.3.0"
    assert release.published_at == datetime(2026, 9, 18, 5, 59, 39, tzinfo=UTC)
    assert release.page_url.endswith("/releases/tag/5.0.3.0")


@pytest.mark.parametrize(
    ("platform", "file_name"),
    [
        (Platform.WINDOWS, "AmneziaVPN_5.0.3.0_windows_x64.exe"),
        (Platform.MACOS, "AmneziaVPN_5.0.3.0_macos_x64.pkg"),
        (Platform.LINUX, "AmneziaVPN_5.0.3.0_linux_x64.run"),
    ],
)
def test_desktop_downloads(payload: dict[str, Any], platform: Platform, file_name: str) -> None:
    (asset,) = parse_release(payload).for_platform(platform)

    assert asset.name == file_name
    assert asset.arch == "x64"
    assert asset.android_version is None
    assert asset.size > 0


def test_eight_android_apks(payload: dict[str, Any]) -> None:
    apks = parse_release(payload).for_platform(Platform.ANDROID)

    assert {(apk.android_version, apk.arch) for apk in apks} == {
        (version, abi)
        for version in ("9-10", "11+")
        for abi in ("arm64-v8a", "armeabi-v7a", "x86", "x86_64")
    }


def test_default_apk_is_android_11_arm64(payload: dict[str, Any]) -> None:
    apk = parse_release(payload).android_apk()

    assert apk is not None
    assert apk.url == DOWNLOAD + "AmneziaVPN_5.0.3.0_android11%2B_arm64-v8a.apk"


def test_apk_by_android_version_and_abi(payload: dict[str, Any]) -> None:
    apk = parse_release(payload).android_apk("9-10", "x86")

    assert apk is not None
    assert apk.name == "AmneziaVPN_5.0.3.0_android9-10_x86.apk"


def test_missing_apk_variant(payload: dict[str, Any]) -> None:
    assert parse_release(payload).android_apk("7-8") is None


def test_unknown_assets_are_skipped(payload: dict[str, Any]) -> None:
    payload["assets"].append(
        {"name": "SHA256SUMS.txt", "size": 1, "browser_download_url": "https://example.com/s"}
    )

    assert len(parse_release(payload).assets) == 11


@pytest.mark.parametrize("broken", [{}, {"tag_name": "1", "published_at": "x", "html_url": "u"}])
def test_unexpected_payload(broken: dict[str, Any]) -> None:
    with pytest.raises(ReleaseFormatError):
        parse_release(broken)
