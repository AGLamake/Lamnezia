"""The visitor's device, guessed from the User-Agent, to open the right guide."""

import re
from dataclasses import dataclass
from typing import Self

from django.db import models

from amnezia_core.releases import Platform


class Device(models.TextChoices):
    WINDOWS = "windows", "Windows"
    MACOS = "macos", "macOS"
    LINUX = "linux", "Linux"
    ANDROID = "android", "Android"
    IOS = "ios", "iOS"

    @property
    def release_platform(self) -> Platform | None:
        """Platform of GitHub release assets; None for iOS, which ships only in the App Store."""
        return None if self is Device.IOS else Platform(self.value)


# Checked in order: Android user agents also mention Linux, iPhone ones mention Mac OS X.
_DEVICE_PATTERNS = (
    (Device.ANDROID, re.compile(r"Android", re.IGNORECASE)),
    (Device.IOS, re.compile(r"iPhone|iPad|iPod", re.IGNORECASE)),
    (Device.WINDOWS, re.compile(r"Windows", re.IGNORECASE)),
    (Device.MACOS, re.compile(r"Macintosh|Mac OS X", re.IGNORECASE)),
    (Device.LINUX, re.compile(r"Linux|X11", re.IGNORECASE)),
)
# Chrome reports "Android 10; K" whatever the real version, so that one counts as unknown.
_ANDROID_VERSION = re.compile(r"Android (\d+)(?!\d|; K\))")
# Amnezia builds separate APKs for Android 9-10 and 11+.
_FIRST_MODERN_ANDROID = 11


@dataclass(frozen=True)
class DetectedDevice:
    """`android_version` uses release asset names ("9-10" or "11+") and is None when unknown."""

    device: Device | None
    android_version: str | None = None

    @classmethod
    def from_user_agent(cls, user_agent: str) -> Self:
        device = next(
            (device for device, pattern in _DEVICE_PATTERNS if pattern.search(user_agent)), None
        )
        if device is not Device.ANDROID:
            return cls(device)
        match = _ANDROID_VERSION.search(user_agent)
        if match is None:
            return cls(device)
        return cls(device, "11+" if int(match[1]) >= _FIRST_MODERN_ANDROID else "9-10")
