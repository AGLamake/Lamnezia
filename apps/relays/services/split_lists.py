"""Network lists of split groups: the daily update of their sources and the files for the relay.

The panel downloads every enabled source once a day, each address once for all the groups
that use it, and keeps the last good list (docs/SPLIT.md 4.5). A list that did not download,
did not read or has less than half of the networks before is not taken: the group keeps the
list it has, the source shows the error, and the administrators hear that the list has not been
updated for so many days. A failed source is tried again at the next hourly run.

The relay gets the merged list of a group and its own domains as files next to the clone
peers; the relay itself never downloads lists. A changed list asks for a relay sync.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from ipaddress import IPv4Network
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import Q, QuerySet

from amnezia_core.relay import DomainList, NetworkList, domains_file, networks_file
from amnezia_core.split_lists import (
    Direction,
    SourceFormat,
    SplitListError,
    Target,
    direct_networks,
)
from apps.groups.models import ServerGroup
from apps.notifications.events import Event
from apps.notifications.services.addresses import AddressGuard
from apps.notifications.services.channels import DeliveryError, HttpClient
from apps.notifications.services.delivery import emit
from apps.relays.models import (
    RIPE_DELEGATIONS_URL,
    SOURCE_ERROR_LENGTH,
    SourceKind,
    SplitSource,
)
from apps.relays.receivers import request_sync

# The RIPE NCC delegations take about 8 MB.
DOWNLOAD_LIMIT = 64 * 1024 * 1024
DOWNLOAD_TIMEOUT = 120
UPDATE_INTERVAL = timedelta(days=1)
_DAY = timedelta(days=1).total_seconds()


def add_default_source(group: ServerGroup) -> SplitSource | None:
    """The RIPE NCC delegations to Russia for a group that has no source yet."""
    if group.split_sources.exists():
        return None
    return SplitSource.objects.create(group=group, url=RIPE_DELEGATIONS_URL, kind=SourceKind.RIPE)


def update_soon() -> None:
    """Update the sources that are due, a new one among them, once the transaction commits."""
    from apps.relays.tasks import update_split_lists  # noqa: PLC0415  the tasks import services

    transaction.on_commit(update_split_lists.delay)


def stored_networks(text: str) -> list[IPv4Network]:
    """The networks a source keeps, one per line as `NetworkList.to_text` writes them."""
    return [IPv4Network(line) for line in text.split()]


@dataclass(frozen=True)
class GroupList:
    """What the relay routes for a split group: the networks sent straight out, merged from the
    enabled sources and the own networks, and the own domains the agent resolves."""

    networks: tuple[IPv4Network, ...]
    domains: DomainList

    @classmethod
    def of(cls, group: ServerGroup) -> "GroupList":
        sources = [
            network
            for source in group.split_sources.all()
            if source.enabled
            for network in stored_networks(source.networks)
        ]
        networks: dict[str, list[IPv4Network]] = {Direction.LOCAL: [], Direction.ABROAD: []}
        domains: dict[str, list[str]] = {Direction.LOCAL: [], Direction.ABROAD: []}
        for entry in group.split_entries.all():
            target = Target.parse(entry.value)
            if target.network is not None:
                networks[entry.direction].append(target.network)
            else:
                domains[entry.direction].append(target.domain)
        merged = direct_networks(sources, networks[Direction.LOCAL], networks[Direction.ABROAD])
        local, abroad = domains[Direction.LOCAL], domains[Direction.ABROAD]
        return cls(merged, DomainList(tuple(sorted(local)), tuple(sorted(abroad))))

    def files(self, port: int) -> dict[str, str]:
        """The list files of the group's port; a group without a list or domains has none, and
        the agent then sends everything but the private networks over the uplink."""
        files = {}
        if self.networks:
            files[networks_file(port)] = NetworkList(self.networks).to_text()
        if self.domains:
            files[domains_file(port)] = self.domains.to_text()
        return files


class SourceUpdate:
    """One run of the update over the sources that are due."""

    def __init__(self, http: HttpClient, now: datetime) -> None:
        self._http = http
        self._now = now

    @classmethod
    def from_settings(cls, now: datetime) -> "SourceUpdate":
        return cls(HttpClient(DOWNLOAD_TIMEOUT, AddressGuard.from_settings()), now)

    def due(self) -> QuerySet[SplitSource]:
        """Enabled sources never checked, checked a day ago or more, or failed last time."""
        stale = Q(checked_at__isnull=True) | Q(checked_at__lte=self._now - UPDATE_INTERVAL)
        found = SplitSource.objects.filter(stale | ~Q(error=""), enabled=True)
        return found.select_related("group")

    def run(self, sources: Iterable[SplitSource]) -> None:
        read: dict[tuple[str, str], tuple[IPv4Network, ...] | str] = {}
        for source in sources:
            key = (source.url, source.kind)
            if key not in read:
                read[key] = self._read(*key)
            self._take(source, read[key])

    def _read(self, url: str, kind: str) -> tuple[IPv4Network, ...] | str:
        """The networks of the list, or why there are none."""
        try:
            body = self._http.get(url, limit=DOWNLOAD_LIMIT)
            return SourceFormat(kind).parse(body.decode("utf-8", errors="replace"))
        except (DeliveryError, SplitListError) as error:
            return str(error)

    def _take(self, source: SplitSource, result: tuple[IPv4Network, ...] | str) -> None:
        if not isinstance(result, str) and len(result) * 2 < source.size:
            result = f"The list has {len(result)} networks, less than half of {source.size}."
        found = SplitSource.objects.filter(pk=source.pk)
        if isinstance(result, str):
            found.update(checked_at=self._now, error=result[:SOURCE_ERROR_LENGTH])
            self._report(source, result)
            return
        text = NetworkList(result).to_text()
        with transaction.atomic():
            found.update(
                networks=text,
                size=len(result),
                updated_at=self._now,
                checked_at=self._now,
                error="",
            )
            if text != source.networks:
                request_sync([source.group.relay_id])

    def _report(self, source: SplitSource, error: str) -> None:
        """Once a day while the source fails; the days count from the last good list."""
        since = source.updated_at or source.created_at
        days = max(1, math.ceil((self._now - since).total_seconds() / _DAY))
        data = {
            "group": source.group.name,
            "source": urlsplit(source.url).hostname or "",
            "days": days,
            "error": error,
        }
        emit(Event.SPLIT_LIST_STALE, data, key=f"split_list_stale:{source.pk}:{self._now.date()}")
