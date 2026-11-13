"""Subscription link: the vless:// links of a user's XRay keys at one address that stays the same.

XRay clients (v2rayN, v2RayTun, Hiddify, Streisand) take a subscription URL and fetch it again
on their own, so a re-issued key or a changed server reaches them without a new link. The body
is the de facto standard: the links one per line, in base64. The Amnezia app has no such
mechanism for self-hosted servers, see docs/DESIGN.md, 3.9.
"""

import base64
import hashlib
import secrets
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from amnezia_core.xray import XRAY_CONTAINER
from apps.accounts.models import User
from apps.core.models import KeyRevealPolicy
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
        self._key = f"keys:subscription-rate:{ip}" if ip else None
        self._limit = settings.SUBSCRIPTION_RATE_LIMIT if limit is None else limit

    def allow(self) -> bool:
        if not self._limit or self._key is None:
            return True
        # add() starts the window only once; incr() is atomic, so parallel fetches count right.
        cache.add(self._key, 0, timeout=self.WINDOW_SECONDS)
        try:
            count = cache.incr(self._key)
        except ValueError:  # the window expired between add() and incr()
            cache.set(self._key, 1, timeout=self.WINDOW_SECONDS)
            count = 1
        return count <= self._limit


@dataclass(frozen=True, repr=False)
class SubscriptionFeed:
    """What a client gets by the link: the user's active XRay keys. The links hold secrets."""

    owner: User
    title: str
    links: tuple[str, ...]

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
        links = tuple(_vless_link(key) for key in keys if _is_xray(key))
        return cls(user, cached_settings.get().site_name, links)

    def body(self) -> bytes:
        return base64.b64encode("\n".join(self.links).encode())

    def headers(self) -> dict[str, str]:
        """Headers the common clients read: the name of the list and how often to refresh it."""
        title = base64.b64encode(self.title.encode()).decode()
        return {
            "profile-title": f"base64:{title}",
            "profile-update-interval": str(UPDATE_INTERVAL_HOURS),
        }


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _is_xray(key: AccessKey) -> bool:
    return key.can_export and profile_container(key.profile) == XRAY_CONTAINER


def _vless_link(key: AccessKey) -> str:
    return XrayKeyProfile.from_json(key.profile).client(key.private_key).vless_link(key.name)
