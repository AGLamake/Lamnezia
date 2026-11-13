"""Users for the admin interface: the list with plans and keys, and the card of one user."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Count, Q, QuerySet

from apps.accounts.models import LinkedAccount, Messenger, User
from apps.accounts.services.linking import links_by_code
from apps.core.services.site_settings import cached_settings
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import instances
from apps.plans.models import Plan, UserLimits
from apps.plans.services.limits import NO_PLAN, EffectiveLimits
from apps.protocols.models import ProtocolInstance
from apps.servers.models import Server
from apps.traffic.services.limits import LimitEnforcer


def users_for_list() -> QuerySet[User]:
    """Users with their plan and the number of their active keys, for `UserRow`."""
    return (
        User.objects.select_related("limits__plan")
        .prefetch_related("limits__plan__servers", "linked_accounts")
        .annotate(active_keys=Count("access_keys", filter=Q(access_keys__status=KeyStatus.ACTIVE)))
        .order_by("username")
    )


def users_on_server(users: QuerySet[User], server: Server) -> QuerySet[User]:
    """Users with a key, not revoked, on any protocol instance of the server."""
    sources = ProtocolInstance.sources_of(server)
    keys = AccessKey.objects.filter(source__in=sources).exclude(status=KeyStatus.REVOKED)
    return users.filter(pk__in=keys.values("user_id"))


def _limits_of(user: User) -> UserLimits | None:
    try:
        return user.limits
    except UserLimits.DoesNotExist:
        return None


def _effective(limits: UserLimits | None) -> EffectiveLimits:
    return EffectiveLimits.of(limits) if limits else NO_PLAN


@dataclass(frozen=True)
class UserRow:
    user: User
    plan: Plan | None
    access_until: datetime | None

    @classmethod
    def rows(cls, users: Iterable[User]) -> list["UserRow"]:
        """Users must come from `users_for_list()`."""
        return [cls.of(user) for user in users]

    @classmethod
    def of(cls, user: User) -> "UserRow":
        limits = _limits_of(user)
        return cls(user, limits.plan if limits else None, _effective(limits).access_until)


@dataclass(frozen=True)
class Usage:
    """Traffic of all the user's keys, in bytes, against the limits."""

    period: int
    total: int
    period_start: datetime | None


@dataclass(frozen=True)
class MessengerSlot:
    """One messenger on the card: the linked account, and whether a link code can be issued."""

    messenger: Messenger
    account: LinkedAccount | None
    invitable: bool

    @classmethod
    def slots(cls, user: User) -> list["MessengerSlot"]:
        site = cached_settings.get()
        linked = {account.messenger: account for account in user.linked_accounts.all()}
        return [
            cls(messenger, linked.get(messenger), links_by_code(site, messenger))
            for messenger in Messenger
        ]


@dataclass(frozen=True)
class UserCard:
    """Everything the admin card shows: plan, personal overrides, limits, traffic and keys."""

    user: User
    limits: UserLimits | None
    effective: EffectiveLimits
    usage: Usage
    keys: list[tuple[AccessKey, str]]
    messengers: list[MessengerSlot]

    @classmethod
    def load(cls, user: User, now: datetime) -> "UserCard":
        limits = (
            UserLimits.objects.select_related("plan")
            .prefetch_related("plan__servers")
            .filter(user=user)
            .first()
        )
        effective = _effective(limits)
        period, total = LimitEnforcer(now).usage(user, effective)
        titles = instances.titles()
        keys = [
            (key, titles.get(key.source, key.source))
            for key in user.access_keys.order_by("status", "-created_at")
        ]
        usage = Usage(period, total, effective.current_period_start(now))
        return cls(user, limits, effective, usage, keys, MessengerSlot.slots(user))

    @property
    def plan(self) -> Plan | None:
        return self.limits.plan if self.limits else None
