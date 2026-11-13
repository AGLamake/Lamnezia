from datetime import datetime, time, timedelta

import pytest
from django.utils import timezone

from apps.servers.models import Server, ServerSample
from apps.servers.services.monitor import daily_history
from apps.servers.services.thinning import HistoryThinner
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

MINUTE = timedelta(minutes=1)


def today() -> datetime:
    return timezone.make_aware(datetime.combine(timezone.localdate(), time()))


def readings(server: Server, start: datetime, count: int, *, error: str = "") -> None:
    ServerSample.objects.bulk_create(
        ServerSample(server=server, at=start + MINUTE * n, error=error, cpu_percent=10.0)
        for n in range(count)
    )


def test_old_days_keep_one_reading_per_quarter_of_an_hour() -> None:
    server = ServerFactory()
    two_days_ago = today() - timedelta(days=2)
    readings(server, two_days_ago, 60)
    readings(server, today(), 60)

    assert HistoryThinner(timedelta(days=1), timezone.now()).thin_all() == 56

    old = server.samples.filter(at__lt=today())
    assert sorted(old.values_list("at", flat=True)) == [
        two_days_ago + timedelta(minutes=15) * n for n in range(4)
    ]
    assert server.samples.filter(at__gte=today()).count() == 60


def test_thinning_keeps_the_availability_of_a_day() -> None:
    server = ServerFactory()
    day = today() - timedelta(days=3)
    readings(server, day, 90)
    readings(server, day + timedelta(minutes=90), 30, error="timeout")
    before = daily_history(server, 7)

    HistoryThinner(timedelta(days=1), timezone.now()).thin_all()

    assert [row.availability for row in daily_history(server, 7)] == [
        row.availability for row in before
    ]


def test_thinning_twice_changes_nothing() -> None:
    server = ServerFactory()
    readings(server, today() - timedelta(days=2), 60)
    thinner = HistoryThinner(timedelta(days=1), timezone.now())
    thinner.thin_all()

    assert thinner.thin_all() == 0
