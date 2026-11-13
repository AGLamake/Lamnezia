"""Size of the tables the housekeeping tasks keep short, and when each task last ran.

The size is measured on PostgreSQL only; another database shows the rows without it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from django.db import connection
from django.db.models import Model
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from apps.core.models import Cleanup
from apps.notifications.models import Delivery, Notification
from apps.operations.models import Operation, OperationStep
from apps.servers.models import ServerSample
from apps.traffic.models import TrafficHourly


@dataclass(frozen=True)
class StoredData:
    """Data a beat task cleans: the rows are counted in the first model, the size in all."""

    title: Promise
    models: tuple[type[Model], ...]
    task: str


@dataclass(frozen=True)
class StorageRow:
    title: Promise
    rows: int
    size: int | None
    cleaned_at: datetime | None
    removed: int | None


STORED_DATA = (
    StoredData(_("Operation log"), (Operation, OperationStep), "operations.prune"),
    StoredData(_("Notification deliveries"), (Delivery, Notification), "notifications.prune"),
    StoredData(_("Hourly traffic"), (TrafficHourly,), "traffic.roll_up"),
    StoredData(_("Server monitoring history"), (ServerSample,), "servers.thin_history"),
)


class StorageReport:
    """One row per kind of stored data, for the statistics page."""

    def __init__(self, data: Sequence[StoredData] = STORED_DATA) -> None:
        self._data = data

    def rows(self) -> list[StorageRow]:
        cleanups = {cleanup.task: cleanup for cleanup in Cleanup.objects.all()}
        report = []
        for data in self._data:
            cleanup = cleanups.get(data.task)
            report.append(
                StorageRow(
                    title=data.title,
                    rows=data.models[0]._default_manager.count(),
                    size=self._size(data.models),
                    cleaned_at=cleanup.ran_at if cleanup else None,
                    removed=cleanup.removed if cleanup else None,
                )
            )
        return report

    @staticmethod
    def _size(models: tuple[type[Model], ...]) -> int | None:
        """Bytes of the tables with their indexes and TOAST; None off PostgreSQL."""
        if connection.vendor != "postgresql":
            return None
        tables = [model._meta.db_table for model in models]
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(pg_total_relation_size(name::regclass)), 0) "
                "FROM unnest(%s::text[]) AS name",
                [tables],
            )
            row = cursor.fetchone()
        return int(row[0]) if row else 0
