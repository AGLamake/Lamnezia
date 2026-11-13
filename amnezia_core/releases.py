"""Amnezia app release from the GitHub API: which download is for which platform."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from amnezia_core.errors import ReleaseFormatError

# Asset names of amnezia-client releases, e.g. AmneziaVPN_5.0.3.0_windows_x64.exe,
# AmneziaVPN_5.0.3.0_android11+_arm64-v8a.apk. Other files (checksums, new formats) are skipped.
_ASSET_NAME = re.compile(
    r"^AmneziaVPN_[^_]+_"
    r"(?:(?P<desktop>windows|macos|linux)_(?P<desktop_arch>[\w-]+)\.(?:exe|pkg|run)"
    r"|android(?P<android>[\d+-]+)_(?P<abi>[\w-]+)\.apk)$"
)

DEFAULT_ANDROID_VERSION = "11+"
DEFAULT_ANDROID_ABI = "arm64-v8a"


class Platform(StrEnum):
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    ANDROID = "android"


@dataclass(frozen=True)
class ReleaseAsset:
    """One download. `android_version` is "9-10" or "11+" for APKs, None for desktop builds."""

    platform: Platform
    arch: str
    name: str
    url: str
    size: int
    android_version: str | None = None


@dataclass(frozen=True)
class AmneziaRelease:
    version: str
    published_at: datetime
    page_url: str
    assets: tuple[ReleaseAsset, ...]

    def for_platform(self, platform: Platform) -> list[ReleaseAsset]:
        return [asset for asset in self.assets if asset.platform == platform]

    def android_apk(
        self, android_version: str = DEFAULT_ANDROID_VERSION, abi: str = DEFAULT_ANDROID_ABI
    ) -> ReleaseAsset | None:
        for asset in self.for_platform(Platform.ANDROID):
            if (asset.android_version, asset.arch) == (android_version, abi):
                return asset
        return None


def parse_release(payload: Mapping[str, Any]) -> AmneziaRelease:
    """Parse a `GET /repos/amnezia-vpn/amnezia-client/releases/latest` response."""
    try:
        return AmneziaRelease(
            version=str(payload["tag_name"]),
            published_at=datetime.fromisoformat(payload["published_at"]),
            page_url=str(payload["html_url"]),
            assets=tuple(
                asset for raw in payload["assets"] if (asset := _parse_asset(raw)) is not None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseFormatError(f"unexpected GitHub release payload: {exc}") from exc


def _parse_asset(raw: Mapping[str, Any]) -> ReleaseAsset | None:
    match = _ASSET_NAME.match(raw["name"])
    if match is None:
        return None
    common = {"name": raw["name"], "url": raw["browser_download_url"], "size": int(raw["size"])}
    if match["desktop"]:
        return ReleaseAsset(Platform(match["desktop"]), match["desktop_arch"], **common)
    return ReleaseAsset(Platform.ANDROID, match["abi"], android_version=match["android"], **common)
