"""The storage section of the statistics: rows, size and the last cleanup of each kind of data."""

from datetime import timedelta

import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.core.models import Cleanup
from apps.dashboard.services.storage import StorageReport
from apps.operations import tasks as operation_tasks
from apps.operations.models import Operation, OperationStatus
from apps.servers import tasks as server_tasks
from apps.traffic.models import TrafficHourly

pytestmark = pytest.mark.django_db


def test_cleanup_tasks_note_when_they_ran() -> None:
    old = timezone.now() - timedelta(days=365)
    Operation.objects.create(kind="test", status=OperationStatus.SUCCEEDED, finished_at=old)

    assert operation_tasks.prune() == 1
    assert server_tasks.thin_history() == 0

    cleanups = {cleanup.task: cleanup for cleanup in Cleanup.objects.all()}
    assert cleanups["operations.prune"].removed == 1
    assert cleanups["servers.thin_history"].removed == 0
    assert timezone.now() - cleanups["operations.prune"].ran_at < timedelta(minutes=1)


def test_a_new_run_replaces_the_last_one() -> None:
    Cleanup.record("traffic.roll_up", 5)
    Cleanup.record("traffic.roll_up", 2)

    assert Cleanup.objects.get().removed == 2


def test_report_counts_rows_and_shows_the_last_cleanup() -> None:
    TrafficHourly.objects.create(
        source="instance:1", public_key="k", hour=timezone.now(), rx_bytes=1, tx_bytes=1
    )
    Cleanup.record("traffic.roll_up", 3)

    rows = {str(row.title): row for row in StorageReport().rows()}

    traffic = rows["Hourly traffic"]
    assert traffic.rows == 1
    assert traffic.removed == 3
    assert traffic.cleaned_at is not None
    assert rows["Operation log"].cleaned_at is None
    if connection.vendor == "postgresql":
        assert traffic.size and traffic.size > 0
    else:
        assert traffic.size is None


def test_statistics_page_shows_the_storage(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))
    Cleanup.record("notifications.prune", 4)

    html = client.get("/stats/").content.decode()

    assert "Notification deliveries" in html
    assert "4 rows removed" in html
    assert "not yet" in html
