"""Reading SiteSettings without a database query on every call."""

from uuid import uuid4

from django.core.cache import cache

from apps.core.models import SiteSettings

_VERSION_KEY = "core:site-settings-version"


class SettingsCache:
    """Keeps SiteSettings in the process and reloads it after any process saves a change.

    The shared cache holds only a version stamp, so decrypted secrets never leave the process.
    """

    def __init__(self) -> None:
        self._settings: SiteSettings | None = None
        self._version: str | None = None

    def get(self) -> SiteSettings:
        version = cache.get(_VERSION_KEY)
        if version is None:
            cache.add(_VERSION_KEY, uuid4().hex, timeout=None)
            version = cache.get(_VERSION_KEY)
        if self._settings is None or version != self._version:
            self._settings, self._version = SiteSettings.load(), version
        return self._settings

    def invalidate(self) -> None:
        cache.set(_VERSION_KEY, uuid4().hex, timeout=None)


cached_settings = SettingsCache()
