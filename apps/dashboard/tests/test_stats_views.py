import csv
import io
from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey, KeyStatus
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.servers.tests.factories import ServerFactory
from apps.traffic.models import TrafficHourly, TrafficSource

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN, username="admin"))
    return client


def key_with_traffic(username: str, name: str, rx: int, tx: int) -> AccessKey:
    key = AccessKey.objects.create(
        user=UserFactory(username=username),
        source="instance:1",
        name=name,
        public_key=f"key-{name}",
        status=KeyStatus.ACTIVE,
    )
    hour = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    TrafficHourly.objects.create(
        source=key.source, public_key=key.public_key, hour=hour, rx_bytes=rx, tx_bytes=tx
    )
    return key


def test_statistics_are_for_the_staff_only(client: Client) -> None:
    client.force_login(UserFactory(role=Role.USER))

    assert client.get(reverse("dashboard:stats")).status_code == 403
    assert client.get(reverse("dashboard:stats-export")).status_code == 403


def test_page_shows_totals_chart_and_top(admin_client: Client) -> None:
    key_with_traffic("alice", "iPhone", rx=10, tx=20)
    key_with_traffic("bob", "Laptop", rx=1, tx=2)

    response = admin_client.get(reverse("dashboard:stats"), {"period": "day"})

    assert response.status_code == 200
    assert (response.context["received"], response.context["sent"]) == (11, 22)
    assert len(response.context["chart"]["labels"]) == 24
    assert [row.user.username for row in response.context["top"]] == ["alice", "bob"]
    assert b'<figure class="bar-chart">' in response.content


def test_days_on_the_chart_are_in_the_genitive_in_russian(admin_client: Client) -> None:
    response = admin_client.get(
        reverse("dashboard:stats"), {"period": "week"}, HTTP_ACCEPT_LANGUAGE="ru"
    )

    day = timezone.localdate()
    genitive = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря"
    months = genitive.split()
    assert response.context["chart"]["labels"][-1] == f"{day.day} {months[day.month - 1]}"


def test_wrong_filters_fall_back_to_the_defaults(admin_client: Client) -> None:
    response = admin_client.get(reverse("dashboard:stats"), {"period": "year", "server": "x"})

    assert response.status_code == 200
    assert len(response.context["chart"]["labels"]) == 7
    assert response.context["scope"].server is None


def test_key_link_narrows_the_page_to_the_key(admin_client: Client) -> None:
    key = key_with_traffic("alice", "iPhone", rx=10, tx=20)
    key_with_traffic("bob", "Laptop", rx=1, tx=2)

    response = admin_client.get(reverse("dashboard:stats"), {"key": key.pk})

    assert response.context["received"] == 10
    assert "iPhone" in response.content.decode()


def test_export_writes_a_row_per_key(admin_client: Client) -> None:
    key_with_traffic("alice", "=HYPERLINK()", rx=10, tx=20)

    response = admin_client.get(reverse("dashboard:stats-export"), {"period": "month"})

    assert response["Content-Type"].startswith("text/csv")
    assert "attachment" in response["Content-Disposition"]
    header, row = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(header) == 6
    assert row[1:3] == ["'=HYPERLINK()", "alice"]
    assert row[4:] == ["10", "20"]


def test_export_leaves_traffic_empty_for_a_key_without_data(admin_client: Client) -> None:
    key_with_traffic("alice", "iPhone", rx=0, tx=0)

    response = admin_client.get(reverse("dashboard:stats-export"))

    _header, row = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert row[4:] == ["", ""]


def _instance(container: str) -> ProtocolInstance:
    return ProtocolInstance.objects.create(
        server=ServerFactory(name="Helsinki"),
        container=container,
        port=443,
        subnet="10.8.1.0/24",
        public_key="pub",
        private_key="priv",
        origin=InstanceOrigin.IMPORTED,
    )


def _uncounted(key: AccessKey, instance: ProtocolInstance) -> None:
    AccessKey.objects.filter(pk=key.pk).update(source=instance.source)
    TrafficHourly.objects.filter(public_key=key.public_key).update(source=instance.source)
    TrafficSource.objects.create(source=instance.source, available=False, checked_at=timezone.now())


def test_page_names_the_server_and_protocol_whose_traffic_is_not_counted(
    admin_client: Client,
) -> None:
    key = key_with_traffic("alice", "iPhone", rx=10, tx=20)
    assert not admin_client.get(reverse("dashboard:stats")).context["unavailable"]

    instance = _instance("amnezia-awg2")
    _uncounted(key, instance)
    response = admin_client.get(reverse("dashboard:stats"))

    [row] = response.context["unavailable"]
    assert row.instance == instance and not row.needs_stats_blocks
    assert f"Helsinki · {instance.title}" in response.content.decode()
    assert reverse("servers:detail", args=[instance.server.pk]).encode() in response.content
    assert b"instance:" not in response.content
    assert response.context["inactive"] == []


def test_xray_without_statistics_points_to_its_settings(admin_client: Client) -> None:
    instance = _instance("amnezia-xray")
    _uncounted(key_with_traffic("alice", "iPhone", rx=10, tx=20), instance)

    response = admin_client.get(reverse("dashboard:stats"))

    assert response.context["unavailable"][0].needs_stats_blocks
    assert reverse("protocols:settings", args=[instance.pk]).encode() in response.content


def test_a_removed_container_is_not_reported(admin_client: Client) -> None:
    instance = _instance("amnezia-awg2")
    _uncounted(key_with_traffic("alice", "iPhone", rx=10, tx=20), instance)
    ProtocolInstance.objects.filter(pk=instance.pk).update(container_removed_at=timezone.now())

    assert admin_client.get(reverse("dashboard:stats")).context["unavailable"] == []


def test_period_links_keep_the_other_filters(admin_client: Client) -> None:
    page = admin_client.get(reverse("dashboard:stats"), {"period": "month", "user": "7"})

    links = {link.query: link.current for link in page.context["periods"]}

    assert links["period=month&user=7"] is True
    assert links["period=week&user=7"] is False
