"""Moving the traffic history of keys to another source when their keys move there.

Keys of a server group move to its new leading server (`apps.groups`): their hourly traffic
goes with them, so limits and statistics keep counting what the keys used before.
"""

from django.db import transaction

from apps.traffic.models import TrafficCounter, TrafficHourly


@transaction.atomic
def move_key_traffic(old: str, new: str) -> None:
    """Hourly rows of `old` become rows of `new`; an hour both have is added up.

    Counters of the peers keep their own server and only change whose keys they count.
    """
    moving = TrafficHourly.objects.select_for_update().filter(source=old)
    taken = {
        (row.public_key, row.hour): row
        for row in TrafficHourly.objects.select_for_update().filter(
            source=new, public_key__in=moving.values("public_key")
        )
    }
    merged: list[int] = []
    for row in moving:
        existing = taken.get((row.public_key, row.hour))
        if existing is None:
            continue
        existing.rx_bytes += row.rx_bytes
        existing.tx_bytes += row.tx_bytes
        merged.append(row.pk)
    TrafficHourly.objects.bulk_update(taken.values(), ["rx_bytes", "tx_bytes"])
    TrafficHourly.objects.filter(pk__in=merged).delete()
    TrafficHourly.objects.filter(source=old).update(source=new)
    TrafficCounter.objects.filter(key_source=old).update(key_source=new)
