"""The events other apps raise reach the routed channels."""

import json
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from apps.accounts.models import Role
from apps.accounts.services.letters import Letters
from apps.accounts.services.registration import Applicant, Registration
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.registration import configure
from apps.core.models import RegistrationMode
from apps.notifications.events import EVENTS, Event
from apps.notifications.models import Notification, Trouble
from apps.notifications.services.reminders import warn_expiring
from apps.notifications.tests.conftest import FakeHttp
from apps.notifications.tests.factories import ChannelFactory
from apps.plans.tests.factories import UserLimitsFactory
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.servers.tests.test_metrics import OUTPUT
from apps.servers.tests.test_monitor import INTERVAL, FakeServers, monitor, poll
from apps.traffic.signals import LimitReason, limit_reached

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("commit")]

NOTICE = timedelta(days=3)


def events_posted(http: FakeHttp) -> list[str]:
    return [json.loads(post.body)["event"] for post in http.posts]


def test_a_server_that_goes_down_is_reported_once(http: FakeHttp) -> None:
    ChannelFactory(events=[Event.SERVER_DOWN])
    server, servers = ServerFactory(name="Germany"), FakeServers()
    now = timezone.now()
    poll(servers, server, now - INTERVAL * 3.5)
    servers.down.add(server.name)
    for step in (2.5, 1.5, 0.5):
        poll(servers, server, now - INTERVAL * step)

    assert events_posted(http) == ["server_down"]
    assert json.loads(http.posts[0].body)["data"]["server"] == "Germany"


def test_a_server_down_again_after_coming_back_is_reported_again(http: FakeHttp) -> None:
    ChannelFactory(events=[Event.SERVER_DOWN])
    server, servers = ServerFactory(), FakeServers()
    now = timezone.now()
    for start in (now - INTERVAL * 10, now - INTERVAL * 4):
        servers.down.discard(server.name)
        poll(servers, server, start)
        servers.down.add(server.name)
        poll(servers, server, start + INTERVAL)
        poll(servers, server, start + INTERVAL * 2)

    assert events_posted(http) == ["server_down", "server_down"]


def test_a_server_answering_again_is_reported_once_after_its_outage(http: FakeHttp) -> None:
    ChannelFactory(events=[Event.SERVER_DOWN, Event.SERVER_UP])
    server, servers = ServerFactory(name="Germany"), FakeServers()
    now = timezone.now()
    poll(servers, server, now - INTERVAL * 3.5)
    servers.down.add(server.name)
    poll(servers, server, now - INTERVAL * 1.5)
    poll(servers, server, now - INTERVAL)
    servers.down.discard(server.name)
    poll(servers, server, now - INTERVAL * 0.5)
    poll(servers, server, now)

    assert events_posted(http) == ["server_down", "server_up"]
    assert json.loads(http.posts[1].body)["data"]["server"] == "Germany"


def test_a_server_answering_again_is_reported_with_nobody_told_it_was_down(
    http: FakeHttp,
) -> None:
    ChannelFactory(events=[Event.SERVER_UP])
    server, servers = ServerFactory(), FakeServers()
    now = timezone.now()
    poll(servers, server, now - INTERVAL * 4)
    servers.down.add(server.name)
    poll(servers, server, now - INTERVAL * 3)
    poll(servers, server, now - INTERVAL * 2)
    servers.down.discard(server.name)
    poll(servers, server, now - INTERVAL)

    assert events_posted(http) == ["server_up"]


def test_a_slow_reading_started_before_the_outage_ends_it(http: FakeHttp) -> None:
    ChannelFactory(events=[Event.SERVER_UP])
    server, servers = ServerFactory(), FakeServers()
    now = timezone.now()
    poll(servers, server, now - INTERVAL * 5)
    slow = Server.objects.select_related("latest_sample").get(pk=server.pk)
    poll(servers, server, now - INTERVAL * 4)
    servers.down.add(server.name)
    poll(servers, server, now - INTERVAL * 3)
    servers.down.discard(server.name)
    http.posts.clear()

    monitor(servers, now - INTERVAL).poll(slow)
    poll(servers, server, now)

    assert events_posted(http) == ["server_up"]
    assert not Trouble.objects.exists()


def test_a_slow_failed_reading_keeps_the_answer_of_a_newer_one() -> None:
    server, servers = ServerFactory(), FakeServers()
    now = timezone.now()
    poll(servers, server, now - INTERVAL * 3)
    slow = Server.objects.select_related("latest_sample").get(pk=server.pk)
    poll(servers, server, now - INTERVAL)
    servers.down.add(server.name)

    monitor(servers, now).poll(slow)

    server.refresh_from_db()
    assert (server.last_seen_at, server.online) == (now - INTERVAL, True)
    assert not Trouble.objects.exists()


def test_a_server_that_never_went_down_is_not_reported_up(http: FakeHttp) -> None:
    ChannelFactory(events=[Event.SERVER_UP])
    server, servers = ServerFactory(), FakeServers()
    now = timezone.now()
    poll(servers, server, now - INTERVAL * 1.5)
    servers.down.add(server.name)
    poll(servers, server, now - INTERVAL)
    servers.down.discard(server.name)
    poll(servers, server, now)

    assert not http.posts


DROPPED = OUTPUT + "@@lamnezia-awg3\ndropped\n"


def test_dropped_awg3_settings_are_reported_once_and_their_return_once(http: FakeHttp) -> None:
    ChannelFactory(events=[Event.AWG3_DROPPED, Event.AWG3_RESTORED])
    server, servers = ServerFactory(name="Germany"), FakeServers()
    now = timezone.now()
    poll(servers, server, now - INTERVAL * 5)
    servers.output = DROPPED
    for step in (4, 3):
        poll(servers, server, now - INTERVAL * step)
    servers.output = OUTPUT
    for step in (2, 1):
        poll(servers, server, now - INTERVAL * step)

    assert events_posted(http) == ["awg3_dropped", "awg3_restored"]
    restored = json.loads(http.posts[1].body)["data"]
    assert restored["server"] == "Germany"
    assert restored["since"] == (now - INTERVAL * 4).isoformat()


def test_awg3_settings_dropped_again_are_reported_again(http: FakeHttp) -> None:
    ChannelFactory(events=[Event.AWG3_DROPPED])
    server, servers = ServerFactory(), FakeServers()
    now = timezone.now()
    for step, output in enumerate((DROPPED, OUTPUT, DROPPED)):
        servers.output = output
        poll(servers, server, now - INTERVAL * (3 - step))

    assert events_posted(http) == ["awg3_dropped", "awg3_dropped"]


def test_awg3_settings_return_is_told_with_nobody_told_of_the_drop(http: FakeHttp) -> None:
    ChannelFactory(events=[Event.AWG3_RESTORED])
    server, servers = ServerFactory(), FakeServers(DROPPED)
    now = timezone.now()
    poll(servers, server, now - INTERVAL)
    servers.output = OUTPUT
    poll(servers, server, now)

    assert events_posted(http) == ["awg3_restored"]


def test_a_removed_amnezia_awg2_forgets_the_drop_untold(http: FakeHttp) -> None:
    ChannelFactory(events=[Event.AWG3_RESTORED])
    server, servers = ServerFactory(), FakeServers(DROPPED)
    now = timezone.now()
    poll(servers, server, now - INTERVAL * 2)
    servers.output = OUTPUT.replace("amnezia-awg2 running\n", "")
    poll(servers, server, now - INTERVAL)
    servers.output = OUTPUT
    poll(servers, server, now)

    assert not http.posts
    assert not Trouble.objects.exists()


FULL_MEMORY = OUTPUT.replace("MemAvailable:     500000 kB", "MemAvailable:     100000 kB")


def test_the_monitor_tells_a_lasting_high_load_once_and_its_end_once(http: FakeHttp) -> None:
    ChannelFactory(events=[Event.SERVER_LOAD_HIGH, Event.SERVER_LOAD_NORMAL])
    server, servers = ServerFactory(name="Germany"), FakeServers(FULL_MEMORY)
    now = timezone.now()
    for step in (7, 6, 5, 4):
        poll(servers, server, now - INTERVAL * step)
    servers.output = OUTPUT
    for step in (3, 2, 1):
        poll(servers, server, now - INTERVAL * step)

    assert events_posted(http) == ["server_load_high", "server_load_normal"]
    high = json.loads(http.posts[0].body)["data"]
    assert (high["resource"], high["percent"], high["threshold"]) == ("memory", 95, 90)
    assert high["since"] == (now - INTERVAL * 7).isoformat()


def test_a_registration_request_is_reported(http: FakeHttp) -> None:
    ChannelFactory(events=[Event.REGISTRATION_PENDING])
    site = configure(registration_mode=RegistrationMode.OPEN_MANUAL)

    Registration(site, Letters("http://panel.example.com/")).register(
        Applicant("alice", "alice@example.com", "a long password 42")
    )

    [post] = http.posts
    assert json.loads(post.body)["data"] == {"user": "alice", "email": "alice@example.com"}


def test_a_reached_limit_is_reported_to_admins_and_the_user(http: FakeHttp) -> None:
    ChannelFactory(events=[Event.LIMIT_REACHED])
    user = UserFactory(role=Role.USER, email="alice@example.com")

    limit_reached.send(sender=None, user=user, reason=LimitReason.TRAFFIC)

    assert events_posted(http) == ["limit_reached"]
    assert [letter.to for letter in mail.outbox] == [["alice@example.com"]]


def test_the_limit_reason_reads_as_words_and_reaches_a_webhook_as_its_code(
    http: FakeHttp,
) -> None:
    ChannelFactory(events=[Event.LIMIT_REACHED])
    user = UserFactory(role=Role.USER, email="alice@example.com", language="en")

    limit_reached.send(sender=None, user=user, reason=LimitReason.TOTAL_TRAFFIC)

    [post] = http.posts
    assert json.loads(post.body)["data"]["reason"] == "total_traffic"
    [letter] = mail.outbox
    assert "the traffic in total is used up" in letter.body
    assert "total_traffic" not in letter.body


def test_an_unknown_limit_reason_is_shown_as_it_is() -> None:
    _, text = EVENTS[Event.LIMIT_REACHED].render({"user": "alice", "reason": "newer"})

    assert text.endswith(": newer.")


def test_the_user_is_warned_once_before_the_access_ends(http: FakeHttp) -> None:
    now = timezone.now()
    soon = UserLimitsFactory(access_until=now + timedelta(days=2), user__email="a@example.com")
    UserLimitsFactory(access_until=now + timedelta(days=10), user__email="b@example.com")
    UserLimitsFactory(access_until=now - timedelta(days=1), user__email="c@example.com")

    warn_expiring(now, NOTICE)
    warn_expiring(now + timedelta(hours=1), NOTICE)

    assert [letter.to for letter in mail.outbox] == [[soon.user.email]]
    assert Notification.objects.get().event == Event.ACCESS_EXPIRING


def test_an_extended_access_is_warned_about_again(http: FakeHttp) -> None:
    now = timezone.now()
    limits = UserLimitsFactory(access_until=now + timedelta(days=2))
    warn_expiring(now, NOTICE)

    limits.access_until = now + timedelta(days=4)
    limits.save()
    warn_expiring(now + timedelta(days=2), NOTICE)

    assert len(mail.outbox) == 2
