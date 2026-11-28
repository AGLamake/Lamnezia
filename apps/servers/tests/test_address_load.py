"""Load on a server's address: active keys and traffic in a day against the thresholds."""

from datetime import datetime, timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.core.models import SiteSettings
from apps.dashboard.services.attention import Attention
from apps.keys.models import AccessKey, KeyStatus
from apps.notifications.events import EVENTS, Event
from apps.notifications.models import Notification
from apps.notifications.tests.factories import ChannelFactory
from apps.protocols.models import ProtocolInstance
from apps.servers.models import AddressAlert, AddressMeasure, Server, ServerSample
from apps.servers.services.address_load import GB, AddressLoad, AddressRules, AddressWatch
from apps.servers.tests.factories import ServerFactory
from apps.traffic.models import TrafficHourly

pytestmark = pytest.mark.django_db

HOUR = timedelta(hours=1)
REPEAT = timedelta(hours=6)


def rules(keys: int | None = None, gb: int | None = None) -> AddressRules:
    return AddressRules({AddressMeasure.KEYS: keys, AddressMeasure.TRAFFIC: gb}, REPEAT)


@pytest.fixture(autouse=True)
def channel() -> None:
    ChannelFactory(events=[Event.ADDRESS_BUSY, Event.ADDRESS_CALM])


@pytest.fixture
def server() -> Server:
    return ServerFactory(name="Germany", host="198.51.100.7")


def instance_of(server: Server) -> ProtocolInstance:
    return ProtocolInstance.objects.get_or_create(
        server=server, container="amnezia-awg2", defaults={"port": 51820}
    )[0]


def keys(server: Server, count: int, status: KeyStatus = KeyStatus.ACTIVE) -> None:
    source = instance_of(server).source
    start = AccessKey.objects.count()
    for number in range(start, start + count):
        AccessKey.objects.create(
            source=source, name=f"phone{number}", public_key=f"key{number}", status=status
        )


def traffic(server: Server, hour: datetime, gigabytes: float) -> None:
    half = round(gigabytes * GB / 2)
    TrafficHourly.objects.create(
        source=instance_of(server).source,
        public_key=f"peer{TrafficHourly.objects.count()}",
        hour=hour,
        rx_bytes=half,
        tx_bytes=half,
    )


def events() -> list[str]:
    return list(Notification.objects.order_by("created_at", "pk").values_list("event", flat=True))


def test_the_thresholds_are_off_by_default() -> None:
    site = SiteSettings.load()

    assert (site.address_keys, site.address_daily_gb) == (None, None)
    assert not AddressRules.from_site().watched


def test_nothing_is_counted_or_kept_while_the_thresholds_are_off(server: Server) -> None:
    keys(server, 5)

    AddressWatch(rules()).check(server, timezone.now())

    assert not AddressAlert.objects.exists()
    assert events() == []


def test_only_active_keys_of_the_servers_instances_count(server: Server) -> None:
    keys(server, 3)
    keys(server, 2, KeyStatus.DISABLED_MANUAL)
    keys(ServerFactory(), 4)

    assert AddressLoad(server, timezone.now()).active_keys() == 3


def test_traffic_counts_both_ways_in_the_last_24_hours(server: Server) -> None:
    now = timezone.now()
    traffic(server, now - 2 * HOUR, 1.5)
    traffic(server, now - 23 * HOUR, 1.0)
    traffic(server, now - 25 * HOUR, 9.0)
    traffic(ServerFactory(), now - HOUR, 9.0)

    assert AddressLoad(server, now).daily_gb() == 2


def test_too_many_keys_are_told_once_and_shown(server: Server) -> None:
    keys(server, 3)
    watch = AddressWatch(rules(keys=3))
    now = timezone.now()

    watch.check(server, now)
    watch.check(server, now + HOUR)

    [alert] = server.busy_address
    assert (alert.measure, alert.value, alert.high_since) == (AddressMeasure.KEYS, 3, now)
    [notification] = Notification.objects.all()
    assert notification.event == Event.ADDRESS_BUSY
    assert notification.data == {
        "server": "Germany",
        "host": "198.51.100.7",
        "measure": "keys",
        "value": 3,
        "threshold": 3,
        "since": now.isoformat(),
    }


def test_a_lasting_load_is_told_again_after_the_repeat(server: Server) -> None:
    traffic(server, timezone.now() - HOUR, 12)
    watch = AddressWatch(rules(gb=10))
    now = timezone.now()

    watch.check(server, now)
    watch.check(server, now + REPEAT)

    assert events() == ["address_busy", "address_busy"]


def test_the_end_of_a_told_load_is_told_once(server: Server) -> None:
    keys(server, 3)
    watch = AddressWatch(rules(keys=3))
    now = timezone.now()
    watch.check(server, now)
    AccessKey.objects.filter(public_key="key0").update(status=KeyStatus.REVOKED)

    watch.check(server, now + HOUR)
    watch.check(server, now + 2 * HOUR)

    assert events() == ["address_busy", "address_calm"]
    assert server.busy_address == []
    assert AddressAlert.objects.get().value == 2


def test_a_load_back_soon_after_its_end_is_not_told_again(server: Server) -> None:
    keys(server, 3)
    watch = AddressWatch(rules(keys=3))
    now = timezone.now()
    watch.check(server, now)
    AccessKey.objects.filter(public_key="key0").update(status=KeyStatus.REVOKED)
    watch.check(server, now + HOUR)
    keys(server, 1)

    watch.check(server, now + 2 * HOUR)

    assert events() == ["address_busy", "address_calm"]
    assert len(server.busy_address) == 1


def test_turning_a_threshold_off_ends_its_alert_quietly(server: Server) -> None:
    keys(server, 3)
    now = timezone.now()
    AddressWatch(rules(keys=3)).check(server, now)

    AddressWatch(rules()).check(server, now + HOUR)

    assert server.busy_address == []
    assert events() == ["address_busy"]


def test_the_message_names_the_measure() -> None:
    data = {"server": "Germany", "host": "198.51.100.7", "measure": "traffic", "value": 42}
    data |= {"threshold": 40, "since": "2026-11-28T10:00:00+00:00"}

    title, text = EVENTS[Event.ADDRESS_BUSY].render(data)

    assert title == "Much load on Germany: GB in a day 42"
    assert "threshold of 40" in text


def test_a_busy_address_needs_attention_and_shows_on_the_card(
    client: Client, server: Server
) -> None:
    admin = UserFactory(role=Role.ADMIN)
    server.record_sample(ServerSample.objects.create(server=server, at=timezone.now()))
    AddressAlert.objects.create(
        server=server, measure=AddressMeasure.TRAFFIC, value=42, high_since=timezone.now()
    )
    AddressAlert.objects.create(server=ServerFactory(), measure=AddressMeasure.KEYS, value=1)
    client.force_login(admin)

    [notice] = [n for n in Attention(admin).notices() if n.key.startswith("busy-address:")]
    card = client.get(reverse("servers:detail", args=[server.pk])).content.decode()

    assert notice.text == "Germany: much load on the address — GB in a day 42"
    assert "Much load on the address: GB in a day 42, at or above the threshold since" in card


def test_the_thresholds_are_set_in_the_site_settings(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))

    page = client.get(reverse("core:settings")).content.decode()

    assert 'name="address_keys"' in page
    assert 'name="address_daily_gb"' in page
