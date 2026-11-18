from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from apps.core.models import SiteSettings
from apps.notifications.events import EVENTS, Event
from apps.notifications.models import Notification
from apps.notifications.tests.factories import ChannelFactory
from apps.servers.models import LoadAlert, LoadResource, Server, ServerSample
from apps.servers.services.load import LoadRules, LoadWatch
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

MINUTE = timedelta(minutes=1)
GB = 1_000_000_000
RULES = LoadRules(
    {LoadResource.CPU: 90, LoadResource.MEMORY: 90, LoadResource.DISK: 90},
    readings=3,
    repeat=timedelta(hours=6),
)


@pytest.fixture(autouse=True)
def channel() -> None:
    ChannelFactory(events=[Event.SERVER_LOAD_HIGH, Event.SERVER_LOAD_NORMAL])


class Readings:
    """A server read once a minute from `start`; each reading is checked as the monitor does."""

    def __init__(self, server: Server, rules: LoadRules = RULES) -> None:
        self.server = server
        self.at = timezone.now() - timedelta(days=1)
        self._watch = LoadWatch(rules)

    def cpu(self, *loads: float) -> None:
        for load in loads:
            self.read(cpu=load)

    def read(self, cpu: float = 10, memory: float = 10, disk: float = 10) -> None:
        self.at += MINUTE
        ServerSample.objects.create(
            server=self.server,
            at=self.at,
            cpu_percent=cpu,
            memory_total=100 * GB,
            memory_available=round((100 - memory) * GB),
            disk_total=100 * GB,
            disk_free=round((100 - disk) * GB),
        )
        self._watch.check(self.server, self.at)

    def wait(self, period: timedelta) -> None:
        self.at += period


def told() -> list[tuple[str, dict[str, object]]]:
    return [(n.event, n.data) for n in Notification.objects.order_by("created_at", "pk")]


def events() -> list[str]:
    return [event for event, _data in told()]


@pytest.fixture
def readings() -> Readings:
    return Readings(ServerFactory(name="Germany", host="198.51.100.7"))


def test_a_short_spike_is_not_told(readings: Readings) -> None:
    readings.cpu(10, 95, 97, 10, 99)

    assert events() == []
    assert readings.server.overloads == []


def test_a_load_high_for_the_readings_in_a_row_is_told_once(readings: Readings) -> None:
    readings.cpu(10, 95)
    first_high = readings.at
    readings.cpu(97, 99, 98, 96)

    assert told() == [
        (
            "server_load_high",
            {
                "server": "Germany",
                "host": "198.51.100.7",
                "resource": "cpu",
                "percent": 99,
                "threshold": 90,
                "since": first_high.isoformat(),
            },
        )
    ]
    [alert] = readings.server.overloads
    assert (alert.resource, alert.high_since) == ("cpu", first_high)


def test_the_end_of_a_high_load_is_told_once_after_the_readings_below(readings: Readings) -> None:
    readings.cpu(95, 96, 97)
    since = readings.server.overloads[0].high_since
    readings.cpu(50, 95, 40, 30)
    assert events() == ["server_load_high"]

    readings.cpu(20, 10)

    assert events() == ["server_load_high", "server_load_normal"]
    _event, data = told()[-1]
    assert (data["percent"], data["since"]) == (20, since.isoformat())  # type: ignore[union-attr]
    assert readings.server.overloads == []


def test_a_lasting_high_load_is_repeated_once_per_repeat(readings: Readings) -> None:
    readings.cpu(95, 96, 97)
    readings.wait(timedelta(hours=3))
    readings.cpu(95)
    readings.wait(timedelta(hours=3))
    readings.cpu(95, 95)

    assert events() == ["server_load_high", "server_load_high"]


def test_a_load_back_above_soon_after_is_not_told_again(readings: Readings) -> None:
    readings.cpu(95, 96, 97, 10, 10, 10)
    readings.cpu(95, 96, 97, 10, 10, 10)

    # The second run was not told, so neither is its end.
    assert events() == ["server_load_high", "server_load_normal"]


def test_a_load_back_above_after_the_repeat_is_told_again(readings: Readings) -> None:
    readings.cpu(95, 96, 97, 10, 10, 10)
    readings.wait(timedelta(hours=6))
    readings.cpu(95, 96, 97)

    assert events() == ["server_load_high", "server_load_normal", "server_load_high"]


def test_memory_and_disk_are_watched_apart(readings: Readings) -> None:
    for _ in range(3):
        readings.read(memory=95, disk=92)

    assert sorted(data["resource"] for _event, data in told()) == ["disk", "memory"]  # type: ignore[type-var]
    assert [alert.resource for alert in readings.server.overloads] == ["memory", "disk"]


def test_a_reading_without_the_value_breaks_the_run(readings: Readings) -> None:
    readings.cpu(95, 96)
    readings.at += MINUTE
    ServerSample.objects.create(server=readings.server, at=readings.at, cpu_percent=None)
    readings.cpu(97)

    assert events() == []


def test_unanswered_readings_do_not_break_the_run(readings: Readings) -> None:
    readings.cpu(95, 96)
    readings.at += MINUTE
    ServerSample.objects.create(server=readings.server, at=readings.at, error="timeout")
    readings.cpu(97)

    assert events() == ["server_load_high"]


def test_a_threshold_turned_off_ends_the_high_load_untold(readings: Readings) -> None:
    readings.cpu(95, 96, 97)
    off = LoadRules({**RULES.thresholds, LoadResource.CPU: 0}, RULES.readings, RULES.repeat)

    Readings(readings.server, off).cpu(99)

    assert events() == ["server_load_high"]
    assert not LoadAlert.objects.filter(high_since__isnull=False).exists()


def test_the_rules_come_from_the_site_settings() -> None:
    site = SiteSettings.load()
    site.load_cpu_percent, site.load_readings, site.load_repeat_hours = 80, 5, 2
    site.save()

    rules = LoadRules.from_site()

    assert rules.thresholds[LoadResource.CPU] == 80
    assert (rules.readings, rules.repeat) == (5, timedelta(hours=2))


def test_the_alert_reads_in_a_message() -> None:
    data = {"server": "Germany", "host": "h", "resource": "memory", "percent": 95}
    data |= {"threshold": 90, "since": datetime(2026, 11, 18, 10).isoformat()}

    title, _text = EVENTS[Event.SERVER_LOAD_HIGH].render(data)

    assert title == "High load on Germany: Memory 95%"
