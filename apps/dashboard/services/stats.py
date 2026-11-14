"""Traffic statistics for the staff (DESIGN.md 6.1): traffic of a server, user or key over a
day, week or month, the top consumers, peers online now and keys without traffic. A moderator's
statistics cover the keys of their zone only.

Hourly rows are summed in the database per hour or per key, never fetched one by one, so the page
stays fast with thousands of keys.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import models
from django.db.models import Exists, OuterRef, QuerySet, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from amnezia_core.xray import XRAY_CONTAINER
from apps.accounts.models import User
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.zone import KeyZone
from apps.protocols.models import ProtocolInstance
from apps.servers.models import Server
from apps.traffic.models import TrafficCounter, TrafficHourly, TrafficSource
from apps.traffic.services.online import OnlineRule

_TOP_SIZE = 10
_LIST_SIZE = 50


class Period(models.TextChoices):
    DAY = "day", _("Day")
    WEEK = "week", _("Week")
    MONTH = "month", _("Month")


_BUCKETS = {Period.DAY: 24, Period.WEEK: 7, Period.MONTH: 30}


@dataclass(frozen=True)
class Window:
    """The buckets a period is drawn in: hours of the last day or local days of the last week
    or month, the current one included."""

    period: Period
    now: datetime

    @property
    def hourly(self) -> bool:
        return self.period == Period.DAY

    @property
    def start(self) -> datetime:
        return self.buckets()[0]

    def buckets(self) -> list[datetime]:
        last = self.bucket_of(self.now)
        step = timedelta(hours=1) if self.hourly else timedelta(days=1)
        return [last - step * back for back in reversed(range(_BUCKETS[self.period]))]

    def bucket_of(self, moment: datetime) -> datetime:
        local = timezone.localtime(moment).replace(minute=0, second=0, microsecond=0)
        return local if self.hourly else local.replace(hour=0)


@dataclass(frozen=True)
class Scope:
    """What the statistics look at; every filter left empty means the whole `zone`, the whole
    panel without one."""

    server: Server | None = None
    user: User | None = None
    key: AccessKey | None = None
    zone: KeyZone | None = None

    def traffic(self, rows: QuerySet[TrafficHourly]) -> QuerySet[TrafficHourly]:
        """Narrow traffic rows, which name a peer by source and public key, to the scope."""
        if self.zone and not self.zone.is_whole:
            zoned = self.zone.keys().filter(
                source=OuterRef("source"), public_key=OuterRef("public_key")
            )
            rows = rows.filter(Exists(zoned))
        if self.server:
            rows = rows.filter(source__in=ProtocolInstance.sources_of(self.server))
        if self.user:
            owned = AccessKey.objects.filter(
                user=self.user, source=OuterRef("source"), public_key=OuterRef("public_key")
            )
            rows = rows.filter(Exists(owned))
        if self.key:
            rows = rows.filter(source=self.key.source, public_key=self.key.public_key)
        return rows

    def keys(self) -> QuerySet[AccessKey]:
        keys = self.zone.keys() if self.zone else AccessKey.objects.all()
        if self.server:
            keys = keys.filter(source__in=ProtocolInstance.sources_of(self.server))
        if self.user:
            keys = keys.filter(user=self.user)
        if self.key:
            keys = keys.filter(pk=self.key.pk)
        return keys

    def users(self) -> QuerySet[User]:
        """Owners of the scope's keys that still work; everyone in the zone without filters."""
        if not (self.server or self.user or self.key):
            return self.zone.users() if self.zone else User.objects.all()
        keys = self.keys().exclude(status=KeyStatus.REVOKED)
        return User.objects.filter(pk__in=keys.values("user_id"))


@dataclass(frozen=True)
class Point:
    """Traffic in one bucket, in bytes; rx is what the server received."""

    at: datetime
    rx: int
    tx: int


@dataclass(frozen=True)
class KeyTraffic:
    """Traffic of a key; `known` is False when its source gives no counters (OpenVPN, IPsec)
    or its last reading failed: the zeros then mean "no data", not "no traffic".
    """

    key: AccessKey
    rx: int
    tx: int
    known: bool = True

    @property
    def total(self) -> int:
        return self.rx + self.tx

    @property
    def has_data(self) -> bool:
        """Whether the zeros, if any, mean no traffic rather than no data."""
        return self.known or bool(self.total)


@dataclass(frozen=True)
class UserTraffic:
    user: User
    total: int


class TrafficStats:
    """Statistics of one scope over one window; each method is one or two queries."""

    def __init__(self, scope: Scope, window: Window, online: OnlineRule) -> None:
        self._scope = scope
        self._window = window
        self._online = online

    def series(self) -> list[Point]:
        """Traffic per bucket, empty buckets included, for the chart."""
        hours = (
            self._traffic()
            .values("hour")
            .annotate(rx=Sum("rx_bytes"), tx=Sum("tx_bytes"))
            .values_list("hour", "rx", "tx")
        )
        sums: dict[datetime, list[int]] = defaultdict(lambda: [0, 0])
        for hour, rx, tx in hours:
            bucket = sums[self._window.bucket_of(hour)]
            bucket[0] += rx
            bucket[1] += tx
        return [Point(at, *sums.get(at, (0, 0))) for at in self._window.buckets()]

    def by_key(self) -> list[KeyTraffic]:
        """Every key of the scope with its traffic in the window, the biggest first."""
        totals = {
            (source, public_key): (rx, tx)
            for source, public_key, rx, tx in self._traffic()
            .values("source", "public_key")
            .annotate(rx=Sum("rx_bytes"), tx=Sum("tx_bytes"))
            .values_list("source", "public_key", "rx", "tx")
        }
        known = set(TrafficSource.objects.filter(available=True).values_list("source", flat=True))
        rows = [
            KeyTraffic(key, *totals.get((key.source, key.public_key), (0, 0)), key.source in known)
            for key in self._scope.keys().select_related("user")
        ]
        return sorted(rows, key=lambda row: (-row.total, row.key.name))

    def online(self) -> list[AccessKey]:
        """Keys online now by `OnlineRule` (DESIGN.md 5.5), on any server of their group."""
        recent = TrafficCounter.objects.filter(
            self._online.filter(self._window.now),
            key_source=OuterRef("source"),
            public_key=OuterRef("public_key"),
        )
        keys = self._scope.keys().exclude(status=KeyStatus.REVOKED).filter(Exists(recent))
        return list(keys.select_related("user").order_by("name"))

    def unavailable_sources(self) -> list["UncountedInstance"]:
        """Instances of the scope whose last reading failed: their traffic is unknown, not zero.

        A container removed from its server is left out: its keys have nothing left to count.
        """
        keys = self._scope.keys().exclude(status=KeyStatus.REVOKED).values("source")
        failed = dict(
            TrafficSource.objects.filter(available=False, source__in=keys).values_list(
                "source", "checked_at"
            )
        )
        pks = [pk for pk in map(ProtocolInstance.pk_of, failed) if pk is not None]
        found = ProtocolInstance.objects.filter(pk__in=pks, container_removed_at__isnull=True)
        rows = [
            UncountedInstance(instance, failed[instance.source])
            for instance in found.select_related("server").only(
                "id", "container", "server__id", "server__name"
            )
        ]
        return sorted(rows, key=lambda row: (row.instance.server.name, row.instance.title))

    def _traffic(self) -> QuerySet[TrafficHourly]:
        rows = TrafficHourly.objects.filter(hour__gte=self._window.start)
        return self._scope.traffic(rows)


@dataclass(frozen=True)
class UncountedInstance:
    """A protocol instance whose traffic the panel could not read at `checked_at`."""

    instance: ProtocolInstance
    checked_at: datetime

    @property
    def needs_stats_blocks(self) -> bool:
        """XRay answers no counters until its server.json has the statistics blocks."""
        return self.instance.container == XRAY_CONTAINER


def top_users(keys: Iterable[KeyTraffic], size: int = _TOP_SIZE) -> list[UserTraffic]:
    """Owners whose keys used the most; keys without an owner are left out."""
    totals: dict[int, UserTraffic] = {}
    for row in keys:
        if row.key.user is None or not row.total:
            continue
        known = totals.get(row.key.user.pk)
        totals[row.key.user.pk] = UserTraffic(
            row.key.user, row.total + (known.total if known else 0)
        )
    return sorted(totals.values(), key=lambda row: -row.total)[:size]


def inactive(keys: Iterable[KeyTraffic], size: int = _LIST_SIZE) -> list[AccessKey]:
    """Active keys that moved no traffic in the window: candidates to revoke.

    Keys without data are left out: no reading is not the same as no traffic.
    """
    idle = [
        row.key
        for row in keys
        if row.key.status == KeyStatus.ACTIVE and row.has_data and not row.total
    ]
    return sorted(idle, key=lambda key: key.created_at)[:size]
