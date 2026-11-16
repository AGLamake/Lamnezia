"""Whose traffic is unknown: keys on an instance or a group member the panel cannot read."""

from django.db.models import F

from apps.traffic.models import TrafficCounter, TrafficSource


def unknown_sources() -> dict[str, set[str]]:
    """The source of keys whose traffic is unknown -> the failed sources that hold them.

    A failed source holds its own keys. A member of a server group holds the keys of the leading
    server (`key_source` of its counters): while it cannot be read, those keys are not counted
    in full either.
    """
    failed = set(TrafficSource.objects.filter(available=False).values_list("source", flat=True))
    held = {source: {source} for source in failed}
    members = (
        TrafficCounter.objects.filter(source__in=failed)
        .exclude(key_source=F("source"))
        .values_list("source", "key_source")
        .distinct()
    )
    for source, key_source in members:
        held.setdefault(key_source, set()).add(source)
    return held
