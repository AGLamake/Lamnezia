"""Thinning of old monitor readings: one reading per quarter of an hour after the detail term."""

from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.servers.models import Server, ServerSample

SLOT = timedelta(minutes=15)


class HistoryThinner:
    """Keeps the first reading of every slot in whole local days older than `detail`.

    Whole days, so a day of the history is either all readings or thinned evenly, and its share
    of answered readings stays what it was.
    """

    def __init__(self, detail: timedelta, now: datetime) -> None:
        local = timezone.localtime(now - detail)
        self._before = timezone.make_aware(datetime.combine(local.date(), time()))

    def thin_all(self) -> int:
        """Delete the extra readings of every server; return how many were deleted."""
        return sum(self.thin(server) for server in Server.objects.all())

    def thin(self, server: Server) -> int:
        readings = server.samples.filter(at__lt=self._before).order_by("at").values_list("pk", "at")
        kept_slots: set[int] = set()
        extra = []
        for pk, at in readings:
            slot = int(at.timestamp() // SLOT.total_seconds())
            if slot in kept_slots:
                extra.append(pk)
            kept_slots.add(slot)
        deleted, _ = ServerSample.objects.filter(pk__in=extra).delete()
        return deleted
