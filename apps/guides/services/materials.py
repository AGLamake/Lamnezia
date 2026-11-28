"""Useful materials: the public list grouped by topic and the weekly check that links still open.

The check goes through `MATERIALS_PROXY` when it is set, so a site blocked where the panel runs
does not count as broken. A link, or a redirect, into a private network is not opened unless the
panel settings allow such addresses (`AddressGuard`), as for webhooks.
"""

import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Self

from django.conf import settings
from django.utils import timezone

from apps.core.services.outbound import OutboundProxy, proxied_opener
from apps.guides.models import MaterialTopic, UsefulMaterial
from apps.notifications.services.addresses import AddressGuard, BlockedAddressError

_USER_AGENT = "Lamnezia"


@dataclass(frozen=True)
class LocalizedMaterial:
    """What the page shows of a material: our description in the visitor's language."""

    material: UsefulMaterial
    language: str

    @property
    def title(self) -> str:
        return self.material.title

    @property
    def url(self) -> str:
        return self.material.url

    @property
    def source(self) -> str:
        return self.material.source

    @property
    def material_language(self) -> str:
        return self.material.get_language_display()

    @property
    def description(self) -> str:
        return self.material.description_in(self.language)

    @property
    def credit(self) -> str:
        return self.material.credit


@dataclass(frozen=True)
class MaterialSection:
    topic: MaterialTopic
    materials: list[LocalizedMaterial]

    @property
    def label(self) -> str:
        return str(self.topic.label)


def sections_by_topic(materials: Iterable[UsefulMaterial], language: str) -> list[MaterialSection]:
    """Sections in the order of the topics; a topic without materials is left out."""
    grouped: dict[str, list[LocalizedMaterial]] = {}
    for material in materials:
        grouped.setdefault(material.topic, []).append(LocalizedMaterial(material, language))
    return [MaterialSection(topic, grouped[topic]) for topic in MaterialTopic if topic in grouped]


@dataclass(frozen=True)
class LinkStatus:
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


Opener = Callable[[urllib.request.Request, float], object]


class GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to an address the guard lets pass."""

    def __init__(self, guard: AddressGuard) -> None:
        self._guard = guard

    def redirect_request(self, *args: Any) -> urllib.request.Request | None:
        # urllib passes (req, fp, code, msg, headers, newurl).
        newurl: str = args[-1]
        try:
            self._guard.check(newurl)
        except BlockedAddressError as error:
            raise urllib.error.URLError(_blocked(error)) from None
        return super().redirect_request(*args)


def status_opener(proxy: OutboundProxy | None, guard: AddressGuard | None = None) -> Opener:
    """Opens a request, through the proxy when there is one, and gives the answer status.

    With a guard, a redirect into a private network is refused.
    """
    open_url = proxied_opener(proxy, *([GuardedRedirects(guard)] if guard else []))

    def open_status(request: urllib.request.Request, timeout: float) -> object:
        with open_url(request, timeout=timeout) as response:
            return response.status

    return open_status


class LinkChecker:
    """Opens a link with HEAD and, if the site refuses HEAD, with GET; redirects are followed."""

    def __init__(
        self,
        timeout: float = 15,
        opener: Opener | None = None,
        guard: AddressGuard | None = None,
    ) -> None:
        self._timeout = timeout
        self._opener = opener or status_opener(None, guard)
        self._guard = guard

    @classmethod
    def from_settings(cls) -> Self:
        proxy = OutboundProxy.parse(settings.MATERIALS_PROXY)
        guard = AddressGuard.from_settings()
        return cls(settings.MATERIALS_CHECK_TIMEOUT, status_opener(proxy, guard), guard)

    def check(self, url: str) -> LinkStatus:
        if self._guard:
            try:
                self._guard.check(url)
            except BlockedAddressError as error:
                return LinkStatus(_blocked(error))
        status = self._try(url, "HEAD")
        return status if status.ok else self._try(url, "GET")

    def _try(self, url: str, method: str) -> LinkStatus:
        request = urllib.request.Request(url, method=method, headers={"User-Agent": _USER_AGENT})
        try:
            self._opener(request, self._timeout)
        except urllib.error.HTTPError as exc:
            return LinkStatus(f"HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return LinkStatus(str(reason)[:200] or type(exc).__name__)
        return LinkStatus()


def _blocked(error: BlockedAddressError) -> str:
    return f"{error.address} is not a public address"


class MaterialLinkCheck:
    """Checks every published material and stores the result for the admin."""

    def __init__(self, checker: LinkChecker) -> None:
        self._checker = checker

    @classmethod
    def from_settings(cls) -> Self:
        return cls(LinkChecker.from_settings())

    def run(self) -> int:
        """Returns how many links are broken now."""
        today = timezone.localdate()
        broken = 0
        for material in UsefulMaterial.objects.published():
            status = self._checker.check(material.url)
            material.checked_at = today
            material.is_broken = not status.ok
            material.check_error = status.error
            material.save(update_fields=["checked_at", "is_broken", "check_error"])
            broken += material.is_broken
        return broken
