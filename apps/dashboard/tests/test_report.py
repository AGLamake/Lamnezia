"""The scheduled report to the staff: when it goes, what it holds and who gets which zone."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, Status
from apps.accounts.tests.factories import UserFactory
from apps.core.models import ReportFrequency, SiteSettings
from apps.dashboard.services.report import StaffReport, StaffReports
from apps.keys.models import AccessKey, KeyStatus
from apps.notifications.events import EVENTS, Event
from apps.notifications.models import Delivery, Notification, Route
from apps.notifications.tests.factories import ChannelFactory
from apps.operations.tests.factories import FailedOperationFactory
from apps.plans.models import UserLimits
from apps.plans.tests.factories import PlanFactory
from apps.servers.models import Server, ServerSample
from apps.servers.tests.factories import ServerFactory
from apps.support.models import Conversation, Kind
from apps.traffic.models import TrafficHourly

pytestmark = pytest.mark.django_db

# A Monday morning, after the report hour.
NOW = timezone.make_aware(datetime(2026, 11, 16, 10, 15))
GB = 1024**3


@pytest.fixture(autouse=True)
def _no_archives(settings: Any, tmp_path: Path) -> None:
    settings.PANEL_ARCHIVE_DIR = tmp_path


def routed() -> None:
    Route.objects.create(channel=ChannelFactory(), event=Event.STAFF_REPORT)


def down(name: str) -> Server:
    server = ServerFactory(name=name)
    server.record_sample(ServerSample.objects.create(server=server, at=timezone.now()))
    Server.objects.filter(pk=server.pk).update(last_seen_at=timezone.now() - timedelta(days=1))
    return server


def key_with_traffic(name: str, amount: int) -> AccessKey:
    key = AccessKey.objects.create(
        user=UserFactory(),
        source="instance:1",
        name=name,
        public_key=f"key-{name}",
        status=KeyStatus.ACTIVE,
    )
    TrafficHourly.objects.create(
        source=key.source,
        public_key=key.public_key,
        hour=(NOW - timedelta(hours=2)).replace(minute=0),
        rx_bytes=amount,
        tx_bytes=0,
    )
    return key


class TestSchedule:
    @pytest.mark.parametrize(
        ("frequency", "now", "due"),
        [
            (ReportFrequency.OFF, NOW, None),
            (ReportFrequency.DAILY, NOW.replace(hour=8), None),
            (ReportFrequency.DAILY, NOW, "2026-11-16"),
            (ReportFrequency.WEEKLY, NOW, "2026-W47"),
            (ReportFrequency.WEEKLY, NOW + timedelta(days=3), "2026-W47"),
        ],
    )
    def test_a_report_is_named_by_its_day_or_week(
        self, frequency: str, now: datetime, due: str | None
    ) -> None:
        assert StaffReports(frequency, now).due() == due

    def test_reports_are_off_by_default(self) -> None:
        assert SiteSettings.load().report_frequency == ReportFrequency.OFF

    def test_the_report_of_the_day_goes_once(self) -> None:
        routed()
        reports = StaffReports(ReportFrequency.DAILY, NOW)

        assert reports.send_due() == 1
        assert reports.send_due() == 0
        assert Notification.objects.filter(event=Event.STAFF_REPORT).count() == 1

    def test_nothing_is_built_while_reports_are_off(self) -> None:
        routed()

        assert StaffReports(ReportFrequency.OFF, NOW).send_due() == 0
        assert not Notification.objects.exists()

    def test_send_now_goes_every_time_even_while_off(self) -> None:
        routed()
        reports = StaffReports(ReportFrequency.OFF, NOW)

        assert reports.send_now() == 1
        assert reports.send_now() == 1
        [first, _second] = Notification.objects.filter(event=Event.STAFF_REPORT)
        assert first.data["period"] == ReportFrequency.DAILY
        assert first.key is None


class TestContent:
    def test_the_channels_get_the_whole_panel(self) -> None:
        routed()
        down("Helsinki")
        ServerFactory(name="Riga")
        key_with_traffic("big", 3 * GB)
        key_with_traffic("small", GB)
        UserFactory(status=Status.PENDING)
        FailedOperationFactory(created_at=NOW - timedelta(hours=1))
        Conversation.objects.create(user=UserFactory(), kind=Kind.TICKET)
        plan = PlanFactory()
        client = UserFactory(username="anna", role=Role.USER, status=Status.ACTIVE)
        UserLimits.objects.create(user=client, plan=plan, access_until=NOW + timedelta(days=2))

        StaffReports(ReportFrequency.DAILY, NOW).send_due()

        [report] = Notification.objects.filter(event=Event.STAFF_REPORT)
        data = report.data
        assert data["servers_down"] == ["Helsinki"]
        assert data["traffic"] == 4 * GB
        assert data["top_keys"] == [["big", 3 * GB], ["small", GB]]
        assert data["access_ending"] == ["anna 2026-11-18"]
        assert data["failed_operations"] == 1
        assert data["applications"] == 1
        assert data["new_requests"] == 1
        assert data["backups_oldest"] is None
        assert data["archive_at"] is None
        assert data["url"].endswith(reverse("dashboard:index"))

    def test_the_report_reads_sizes_and_dashes(self) -> None:
        data = StaffReport(UserFactory(role=Role.ADMIN), ReportFrequency.WEEKLY, NOW).data()
        data |= {"traffic": GB, "top_keys": [["phone", GB]]}

        title, text = EVENTS[Event.STAFF_REPORT].render(data)

        assert title.startswith("Panel report since 2026-11-10")
        assert "Traffic: 1.0\xa0GB" in text
        assert "Most active keys: phone (1.0\xa0GB)" in text
        assert "Servers down: —" in text
        assert "Latest panel archive: —" in text


class TestModerators:
    def test_a_moderator_gets_only_their_zone(self) -> None:
        moderator = UserFactory(role=Role.MODERATOR, email="mod@example.com")
        mine = down("Mine")
        down("Theirs")
        moderator.moderated_servers.add(mine)
        Conversation.objects.create(user=UserFactory(manager=moderator), kind=Kind.TICKET)
        Conversation.objects.create(user=UserFactory(), kind=Kind.TICKET)
        key_with_traffic("not-mine", GB)

        assert StaffReports(ReportFrequency.DAILY, NOW).send_due() == 1

        [report] = Notification.objects.filter(event=Event.ZONE_REPORT)
        assert report.user == moderator
        assert report.data["servers_down"] == ["Mine"]
        assert report.data["new_requests"] == 1
        assert report.data["traffic"] == 0
        assert report.data["top_keys"] == []
        assert "applications" not in report.data
        assert "relays_down" not in report.data
        [delivery] = Delivery.objects.all()
        assert delivery.to_user
        assert delivery.channel is None

    def test_each_moderator_gets_the_report_once(self) -> None:
        UserFactory(role=Role.MODERATOR, email="one@example.com")
        UserFactory(role=Role.MODERATOR, email="two@example.com")
        UserFactory(role=Role.MODERATOR, email="")
        reports = StaffReports(ReportFrequency.WEEKLY, NOW)

        assert reports.send_due() == 2
        assert reports.send_due() == 0


class TestSendNow:
    @patch("apps.dashboard.views.send_reports_now.delay")
    def test_an_administrator_sends_the_report_now(self, delay: MagicMock, client: Client) -> None:
        client.force_login(UserFactory(role=Role.ADMIN))

        response = client.post(reverse("dashboard:report-send"))

        assert response.url == reverse("notifications:log")  # type: ignore[attr-defined]
        delay.assert_called_once_with()

    @patch("apps.dashboard.views.send_reports_now.delay")
    def test_a_moderator_may_not(self, delay: MagicMock, client: Client) -> None:
        client.force_login(UserFactory(role=Role.MODERATOR))

        response = client.post(reverse("dashboard:report-send"))

        assert response.status_code == 403
        delay.assert_not_called()

    def test_the_settings_page_has_the_report_section(self, client: Client) -> None:
        client.force_login(UserFactory(role=Role.ADMIN))

        page = client.get(reverse("core:settings")).content.decode()

        assert 'name="report_frequency"' in page
        assert reverse("dashboard:report-send") in page
