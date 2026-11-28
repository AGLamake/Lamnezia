"""The channel load history of a relay and the traffic of its split groups: buckets of the
charts, thinning, pruning, the card and the group page."""

from datetime import datetime, time, timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from amnezia_core.relay import Routing
from apps.accounts.models import User
from apps.core.models import Cleanup
from apps.groups.models import GroupRouting, ServerGroup
from apps.relays.models import Relay, RelaySample, SplitSample
from apps.relays.services.health import GroupHealth, RelayHealth, SplitTraffic
from apps.relays.services.load import DAY, LoadHistory, SplitHistory, prune_load
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
    # The switch works without JS: CSS shows the chart of the checked period.
    assert 'class="period-switch' in page
    assert 'data-period="week"' in page
    assert "x-show" not in page.split('class="period-switch', 1)[1].split("</figure>", 1)[0]
    assert "No data" in page  # the hours before the readings
    assert "<td>2.0\xa0KB/s</td>" in page  # from the clients in the chart table


def test_the_load_chart_has_a_bar_per_bucket_from_its_start(relay: Relay) -> None:
    readings(relay, NOW - timedelta(minutes=30), 1, rate=100.4)

    chart = load_chart(LoadHistory(relay, NOW).series(DAY, 24), DAY, "H:i")

    assert len(chart["labels"]) == 24
    assert chart["labels"][-1] == date_format(timezone.localtime(NOW - timedelta(hours=1)), "H:i")
    assert chart["series"] == [[None] * 23 + [100], [None] * 23 + [201]]  # no readings: a gap


def split_readings(group: ServerGroup, start: datetime, count: int, abroad: float = 300.0) -> None:
    SplitSample.objects.bulk_create(
        SplitSample(group=group, at=start + MINUTE * n, rate_abroad=abroad, rate_direct=100.0)
        for n in range(count)
    )


def splitting(relay: Relay, group: ServerGroup, traffic: SplitTraffic | None) -> None:
    """The relay was read with the group split and, when given, its traffic known."""
    health = GroupHealth(
        port=group.port,
        active=None,
        switched_at=None,
        alive=(),
        down=(),
        bytes_from_clients=0,
        bytes_to_clients=0,
        routing=Routing.SPLIT,
        mode=Routing.SPLIT,
        traffic=traffic,
    )
    relay.health = RelayHealth(int(timezone.now().timestamp()), {group.port: health}).to_dict()
    relay.read_at = timezone.now()
    relay.save(update_fields=["health", "read_at"])


def test_the_split_groups_of_one_reading_add_up(relay: Relay, relayed: ServerGroup) -> None:
    other = ServerGroup.objects.create(name="Other", port=relayed.port + 1, relay=relay.server)
    split_readings(relayed, NOW - timedelta(minutes=30), 2, abroad=300.0)
    split_readings(other, NOW - timedelta(minutes=30), 1, abroad=500.0)

    series = SplitHistory.of_relay(relay, NOW).series(DAY, 24)
    only = SplitHistory(relayed.split_samples.all(), NOW).series(DAY, 24)

    # Two readings: 300 + 500 and 300 alone, both with 100 straight out of each group.
    assert series.abroad == [None] * 23 + [550.0]
    assert series.direct == [None] * 23 + [150.0]
    assert only.abroad == [None] * 23 + [300.0]
    assert only.peak_direct == 100.0


def test_split_readings_are_thinned_and_deleted(relay: Relay, relayed: ServerGroup) -> None:
    today = timezone.make_aware(datetime.combine(timezone.localdate(), time()))
    split_readings(relayed, today - timedelta(days=9), 3)
    split_readings(relayed, today - timedelta(days=2), 60)

    prune_load(today - timedelta(days=7))
    thin_history()

    assert relayed.split_samples.count() == 4


def test_the_relay_card_shows_the_split_traffic(
    client: Client, admin: User, relay: Relay, relayed: ServerGroup
) -> None:
    splitting(relay, relayed, SplitTraffic(abroad=3072.0, direct=1024.0))
    split_readings(relayed, timezone.now() - timedelta(minutes=10), 3)
    client.force_login(admin)

    page = client.get(reverse("servers:detail", args=[relay.server.pk])).content.decode()

    section = page.split(f'id="relay-{relay.pk}-split"', 1)[1]
    assert "3.0\xa0KB/s abroad through the channel, 1.0\xa0KB/s straight from Russia" in section
    assert section.count('class="bar-chart"') == 2  # the day and the week behind the switch
    assert "Through the channel" in section and "Straight out" in section
    assert f'name="relay-{relay.pk}-split"' in section


def test_the_group_page_shows_its_split_traffic(
    client: Client, admin: User, relay: Relay, relayed: ServerGroup
) -> None:
    relayed.routing = GroupRouting.SPLIT
    relayed.save(update_fields=["routing"])
    splitting(relay, relayed, None)
    split_readings(relayed, timezone.now() - timedelta(minutes=10), 3)
    client.force_login(admin)

    page = client.get(reverse("groups:detail", args=[relayed.pk])).content.decode()

    section = page.split(f'id="group-{relayed.pk}-split"', 1)[1]
    assert "is not known yet" in section
    assert section.count('class="bar-chart"') == 2


def test_a_forwarding_relay_shows_no_split_traffic(
    client: Client, admin: User, relay: Relay
) -> None:
    relay.read_at = timezone.now()
    relay.save(update_fields=["read_at"])
    client.force_login(admin)

    page = client.get(reverse("servers:detail", args=[relay.server.pk])).content.decode()

    assert "-split" not in page
