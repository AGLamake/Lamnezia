"""Whose traffic is unknown: keys on an instance or a group member the panel cannot read."""

from collections.abc import Iterable
from itertools import chain

from django.db.models import F

from apps.keys.models import GONE_STATUSES, AccessKey
from apps.traffic.models import TrafficCounter, TrafficSource


def unknown_sources() -> dict[str, set[str]]:
    """The source of keys whose traffic is unknown -> the failed sources that hold them.

    A failed source holds its own keys. A member of a server group holds the keys of the leading
    server (`key_source` of the source, or of its counters from before it was stored): while it
    cannot be read, those keys are not counted in full either.
    """
    rows = TrafficSource.objects.filter(available=False).values_list("source", "key_source")
    failed = dict(rows)
    held = {source: {source} for source in failed}
    counted = (
        TrafficCounter.objects.filter(source__in=failed)
        .exclude(key_source=F("source"))
        .values_list("source", "key_source")
        .distinct()
    )
    stored = ((source, key) for source, key in failed.items() if key)
    for source, key_source in chain(stored, counted):
        held.setdefault(key_source, set()).add(source)
    return held


def users_with_unknown_traffic(user_ids: Iterable[int]) -> set[int]:
    """Users among `user_ids` with a live key whose traffic is unknown: their counted traffic,
    and a forecast from it, may be below the real one."""
    unknown = unknown_sources()
    if not unknown:
        return set()
    keys = AccessKey.objects.filter(user_id__in=user_ids, source__in=unknown.keys()).exclude(
        status__in=GONE_STATUSES
    )
    return set(keys.values_list("user_id", flat=True))
