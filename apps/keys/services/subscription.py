"""Subscription link: the vless:// links of a user's XRay keys at one address that stays the same.

XRay clients (v2rayN, v2RayTun, Hiddify, Streisand) take a subscription URL and fetch it again
on their own, so a re-issued key or a changed server reaches them without a new link. The body
is the de facto standard: the links one per line, in base64. Clash/Mihomo and sing-box clients
get a whole profile of their own format instead (`FeedFormat`). The Amnezia app has no such
mechanism for self-hosted servers, see docs/DESIGN.md, 3.9.
"""

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

from django.conf import settings

from amnezia_core.xray import XRAY_CONTAINER, XrayClientProfile
from amnezia_core.xray_formats import XrayProxyList
from apps.accounts.models import User
from apps.core.models import KeyRevealPolicy
from apps.core.services.rate import RateWindow
from apps.core.services.site_settings import cached_settings
from apps.keys.models import AccessKey, KeyStatus, Subscription
from apps.keys.services.profile import XrayKeyProfile, profile_container
from apps.keys.services.xray_profiles import request_profiles

# Hours between fetches, for the clients that read `profile-update-interval`.
UPDATE_INTERVAL_HOURS = 12


def subscriptions_allowed() -> bool:
    """The link shows the keys on every fetch, so it exists only while keys may be shown any
    time.
    """
    return cached_settings.get().key_reveal_policy == KeyRevealPolicy.ALWAYS


class MySubscription:
    """The subscription link of one user: show, replace, delete."""

    def __init__(self, user: User) -> None:
        self._user = user

    @property
    def available(self) -> bool:
        return self._user.can_have_keys and subscriptions_allowed()

    def token(self) -> str | None:
        subscription = Subscription.objects.filter(user=self._user).first()
        return subscription.token if subscription else None

    def renew(self) -> str:
        """A new token; the previous link stops working at once."""
        token = secrets.token_urlsafe(32)
        Subscription.objects.update_or_create(
            user=self._user, defaults={"token": token, "digest": _digest(token)}
        )
        return token

    def delete(self) -> None:
        Subscription.objects.filter(user=self._user).delete()


class SubscriptionThrottle:
    """At most SUBSCRIPTION_RATE_LIMIT fetches a minute from one address (0: no limit).

    Clients fetch every few hours; a burst is someone trying tokens or a broken client.
    """

    WINDOW_SECONDS = 60

    def __init__(self, ip: str | None, limit: int | None = None) -> None:
        limit = settings.SUBSCRIPTION_RATE_LIMIT if limit is None else limit
        self._window = (
            RateWindow(f"keys:subscription-rate:{ip}", limit, self.WINDOW_SECONDS) if ip else None
        )

    def allow(self) -> bool:
        return self._window is None or self._window.hit()


class FeedFormat(StrEnum):
    """How the feed is written: the `format` parameter, else guessed from the User-Agent."""

    RAW = "raw"
    CLASH = "clash"
    SINGBOX = "singbox"

    @classmethod
    def choose(cls, requested: str, user_agent: str) -> "FeedFormat":
        """An unknown parameter and an unknown client get the links: every client reads them."""
        named = _FORMAT_NAMES.get(requested.strip().lower())
        if named is not None:
            return named
        for pattern, kind in _USER_AGENTS:
            if pattern.search(user_agent):
                return kind
        return cls.RAW

    @property
    def content_type(self) -> str:
        return _CONTENT_TYPES[self]


_FORMAT_NAMES = {
    "raw": FeedFormat.RAW,
    "base64": FeedFormat.RAW,
    "clash": FeedFormat.CLASH,
    "mihomo": FeedFormat.CLASH,
    "singbox": FeedFormat.SINGBOX,
    "sing-box": FeedFormat.SINGBOX,
}
# Order matters: Hiddify names Clash and sing-box in its User-Agent but reads the links best.
_USER_AGENTS = (
    (re.compile(r"hiddify|v2ray|xray|streisand|happ", re.IGNORECASE), FeedFormat.RAW),
    (re.compile(r"sing-?box|^SF[AIMT]/", re.IGNORECASE), FeedFormat.SINGBOX),
    (re.compile(r"clash|mihomo|stash", re.IGNORECASE), FeedFormat.CLASH),
)
_CONTENT_TYPES = {
    FeedFormat.RAW: "text/plain; charset=utf-8",
    FeedFormat.CLASH: "text/yaml; charset=utf-8",
    FeedFormat.SINGBOX: "application/json; charset=utf-8",
}


@dataclass(frozen=True, repr=False)
class SubscriptionFeed:
    """What a client gets by the link: the user's active XRay keys. The profiles hold secrets."""

    owner: User
    title: str
    profiles: tuple[tuple[str, XrayClientProfile], ...]

    @classmethod
    def find(cls, token: str) -> "SubscriptionFeed | None":
        """The feed of the token; None when it is unknown or its owner may have no keys."""
        subscription = (
            Subscription.objects.select_related("user").filter(digest=_digest(token)).first()
        )
        if subscription is None or not subscriptions_allowed():
            return None
        user = subscription.user
        if not user.is_active or not user.can_have_keys:
            return None
        keys = list(user.access_keys.filter(status=KeyStatus.ACTIVE).order_by("created_at"))
        request_profiles(keys)  # imported keys join the feed on one of the next refreshes
        profiles = tuple((key.name, _client_profile(key)) for key in keys if _is_xray(key))
        return cls(user, cached_settings.get().site_name, profiles)

    @property
    def links(self) -> tuple[str, ...]:
        return tuple(profile.vless_link(name) for name, profile in self.profiles)

    def body(self, kind: FeedFormat = FeedFormat.RAW) -> bytes:
        if kind is FeedFormat.CLASH:
            return XrayProxyList.of(self.title, self.profiles).clash().encode()
        if kind is FeedFormat.SINGBOX:
            return XrayProxyList.of(self.title, self.profiles).singbox().encode()
        return base64.b64encode("\n".join(self.links).encode())

    def headers(self, kind: FeedFormat = FeedFormat.RAW) -> dict[str, str]:
        """Headers the common clients read: the name of the list and how often to refresh it.

        Clash clients name a profile after the file of `Content-Disposition`.
        """
        title = base64.b64encode(self.title.encode()).decode()
        headers = {
            "profile-title": f"base64:{title}",
            "profile-update-interval": str(UPDATE_INTERVAL_HOURS),
        }
        if kind is FeedFormat.CLASH:
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(self.title)}"
        return headers


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _is_xray(key: AccessKey) -> bool:
    return key.can_export and profile_container(key.profile) == XRAY_CONTAINER


def _client_profile(key: AccessKey) -> XrayClientProfile:
    return XrayKeyProfile.from_json(key.profile).client(key.private_key)
