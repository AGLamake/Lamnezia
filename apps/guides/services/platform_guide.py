"""What the guide page for one device offers: release downloads, APK choice, store link."""

from dataclasses import dataclass
from typing import Self

from amnezia_core.releases import (
    DEFAULT_ANDROID_ABI,
    DEFAULT_ANDROID_VERSION,
    AmneziaRelease,
    Platform,
    ReleaseAsset,
)
from apps.guides.services.devices import Device
from apps.guides.services.releases import StoreLinks

RELEASES_PAGE_URL = "https://github.com/amnezia-vpn/amnezia-client/releases/latest"


@dataclass(frozen=True)
class ApkChoice:
    """Android version and ABI asked for by the visitor; empty values mean "the default"."""

    android_version: str = ""
    abi: str = ""


@dataclass(frozen=True)
class PlatformGuide:
    """`downloads` holds the desktop builds or the chosen APK, always from the latest release."""

    device: Device
    release: AmneziaRelease | None
    downloads: tuple[ReleaseAsset, ...] = ()
    store_url: str = ""
    apk: ApkChoice | None = None
    android_versions: tuple[str, ...] = ()
    abis: tuple[str, ...] = ()
    releases_page_url: str = RELEASES_PAGE_URL

    @classmethod
    def build(
        cls,
        device: Device,
        release: AmneziaRelease | None,
        stores: StoreLinks,
        apk: ApkChoice | None = None,
    ) -> Self:
        if device is Device.IOS:
            return cls(device, None, store_url=stores.app_store)
        if device is Device.ANDROID:
            return cls._android(release, stores, apk or ApkChoice())
        platform = device.release_platform
        assets = tuple(release.for_platform(platform)) if release and platform else ()
        return cls(device, release, downloads=assets)

    @classmethod
    def _android(
        cls, release: AmneziaRelease | None, stores: StoreLinks, wanted: ApkChoice
    ) -> Self:
        if release is None:
            return cls(Device.ANDROID, None, store_url=stores.google_play)
        apks = release.for_platform(Platform.ANDROID)
        versions = tuple(sorted({apk.android_version or "" for apk in apks}))
        abis = tuple(sorted({apk.arch for apk in apks}))
        # Unknown values (old links, hand-edited URLs) fall back to the build most phones need.
        version = wanted.android_version
        chosen = ApkChoice(
            version if version in versions else DEFAULT_ANDROID_VERSION,
            wanted.abi if wanted.abi in abis else DEFAULT_ANDROID_ABI,
        )
        asset = release.android_apk(chosen.android_version, chosen.abi)
        return cls(
            Device.ANDROID,
            release,
            downloads=(asset,) if asset else (),
            store_url=stores.google_play,
            apk=chosen,
            android_versions=versions,
            abis=abis,
        )
