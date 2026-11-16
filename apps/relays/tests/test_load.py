"""The channel load history of a relay: buckets of the chart, thinning, pruning and the card."""

from datetime import datetime, time, timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from apps.accounts.models import User
from apps.core.models import Cleanup
from apps.groups.models import ServerGroup
from apps.relays.models import Relay, RelaySample
from apps.relays.services.load import DAY, LoadHistory, prune_load
from apps.relays.tasks import thin_history
from apps.relays.templatetags.relays import load_chart

pytestmark = pytest.mark.django_db

NOW = timezone.make_aware(datetime(2026, 11, 16, 12, 0))
MINUTE = timedelta(minutes=1)


@pytest.fixture
def relay(relayed: ServerGroup) -> Relay:
    assert relayed.relay is not None
    return Relay.objects.create(server=relayed.relay, installed_at=NOW - timedelta(days=8))


def readings(relay: Relay, start: datetime, count: int, rate: float = 100.0) -> None:
    RelaySample.objects.bulk_create(
        RelaySample(
            relay=relay, at=start + MINUTE * n, rate_from_clients=rate, rate_to_clients=2 * rate
        )
        for n in range(count)
    )


def test_readings_are_averaged_per_bucket(relay: Relay) -> None:
    readings(relay, NOW - timedelta(hours=1), 2, rate=100.0)
    readings(relay, NOW - timedelta(minutes=30), 1, rate=400.0)
    readings(relay, NOW - timedelta(days=2), 1)  # outside the day

    series = LoadHistory(relay, NOW).series(DAY, 24)

    assert series.since == NOW - DAY
    assert series.from_clients == [None] * 23 + [200.0]
    assert series.to_clients == [None] * 23 + [400.0]
    assert series.peak_from_clients == 200.0
    assert not series.empty


def test_a_relay_without_readings_has_an_empty_series(relay: Relay) -> None:
    series = LoadHistory(relay, NOW).series(DAY, 24)

    assert series.empty
    assert series.peak_to_clients is None


def test_old_readings_are_deleted(relay: Relay) -> None:
    readings(relay, NOW - timedelta(days=8), 3)
    readings(relay, NOW - timedelta(days=1), 3)

    prune_load(NOW - timedelta(days=7))

    assert relay.samples.count() == 3


def test_old_days_keep_one_reading_per_quarter_of_an_hour(relay: Relay) -> None:
    today = timezone.make_aware(datetime.combine(timezone.localdate(), time()))
    readings(relay, today - timedelta(days=2), 60)
    readings(relay, today, 60)

    assert thin_history() == 56

    assert relay.samples.filter(at__lt=today).count() == 4
    assert relay.samples.filter(at__gte=today).count() == 60
    assert Cleanup.objects.get(task="relays.thin_history").removed == 56


def test_the_relay_card_shows_the_load_chart(client: Client, admin: User, relay: Relay) -> None:
    relay.read_at = timezone.now()
    relay.save(update_fields=["read_at"])
    readings(relay, timezone.now() - timedelta(minutes=10), 3, rate=2048.0)
    client.force_login(admin)

    page = client.get(reverse("servers:detail", args=[relay.server.pk])).content.decode()

    assert "From clients, week" in page
    assert "4.0\xa0KB/s" in page  # the peak to the clients
    assert page.count('class="sparkline"') == 4
    assert page.count('class="bar-chart"') == 2  # the day and the week behind the switch
    assert 'value="week" x-model="period"' in page
    assert "<td>2.0\xa0KB/s</td>" in page  # from the clients in the chart table


def test_the_load_chart_has_a_bar_per_bucket_from_its_start(relay: Relay) -> None:
    readings(relay, NOW - timedelta(minutes=30), 1, rate=100.4)

    chart = load_chart(LoadHistory(relay, NOW).series(DAY, 24), DAY, "H:i")

    assert len(chart["labels"]) == 24
    assert chart["labels"][-1] == date_format(timezone.localtime(NOW - timedelta(hours=1)), "H:i")
    assert chart["series"] == [[0] * 23 + [100], [0] * 23 + [201]]
