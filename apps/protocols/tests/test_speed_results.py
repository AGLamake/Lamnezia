"""History of browser speed tests: what the page sends, what is kept and the card summary."""

from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import SpeedTestResult
from apps.protocols.services import speed_test
from apps.protocols.services.speed_results import KEEP, prune, summary
from apps.protocols.services.speed_test import SpeedTestMode, SpeedTestPlan
from apps.protocols.tasks import prune_speed_results
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def start(server: Server, mode: str = SpeedTestMode.BROWSER) -> SpeedTestPlan:
    plan = SpeedTestPlan.new((5542, 55424), 10, mode)
    Operation.objects.create(
        kind=speed_test.START_KIND,
        params={"server_id": server.pk, **plan.to_params()},
        status=OperationStatus.SUCCEEDED,
        finished_at=timezone.now(),
    )
    return plan


def url(server: Server) -> str:
    return f"/protocols/speed-test/{server.pk}/result/"


def send(client: Client, server: Server, token: str, /, **data: str) -> int:
    payload = {
        "token": token,
        "port": "5542",
        "reachable": "on",
        "download": "93.4",
        "upload": "41",
    }
    return client.post(url(server), {**payload, **data}).status_code


def test_a_port_the_page_measured_shows_on_the_server_card(admin_client: Client) -> None:
    server = ServerFactory()
    plan = start(server)

    assert send(admin_client, server, plan.token) == 200

    result = SpeedTestResult.objects.get()
    assert (result.port, result.reachable, result.download, result.upload) == (5542, True, 93.4, 41)
    assert result.mode == SpeedTestMode.BROWSER
    page = admin_client.get(f"/servers/{server.pk}/").content.decode()
    assert "Speed tests by port" in page
    assert "93.4 Mbit/s" in page


def test_nothing_about_the_sender_is_kept() -> None:
    fields = {field.name for field in SpeedTestResult._meta.get_fields()}

    assert fields == {
        "id",
        "server",
        "test",
        "port",
        "transport",
        "mode",
        "reachable",
        "download",
        "upload",
        "measured_at",
    }


def test_a_port_without_an_answer_keeps_no_speeds(admin_client: Client) -> None:
    server = ServerFactory()
    plan = start(server)

    assert send(admin_client, server, plan.token, reachable="") == 200

    result = SpeedTestResult.objects.get()
    assert not result.reachable
    assert result.download is None
    assert result.upload is None


def test_measuring_a_port_again_replaces_its_row(admin_client: Client) -> None:
    server = ServerFactory()
    plan = start(server)

    send(admin_client, server, plan.token)
    send(admin_client, server, plan.token, download="12")

    assert SpeedTestResult.objects.get().download == 12


@pytest.mark.parametrize(
    "data",
    [
        {"token": "wrong"},
        {"port": "6000"},
        {"download": "1000000"},
        {"port": "0"},
    ],
)
def test_a_result_off_the_running_test_is_refused(
    admin_client: Client, data: dict[str, str]
) -> None:
    server = ServerFactory()
    plan = start(server)

    assert send(admin_client, server, plan.token, **data) == 400
    assert not SpeedTestResult.objects.exists()


def test_without_a_running_browser_test_nothing_is_saved(admin_client: Client) -> None:
    idle = ServerFactory()
    iperf3 = ServerFactory()
    plan = start(iperf3, SpeedTestMode.IPERF3)

    assert send(admin_client, idle, "token") == 404
    assert send(admin_client, iperf3, plan.token) == 400
    assert not SpeedTestResult.objects.exists()


def test_a_guest_cannot_send_results(client: Client) -> None:
    server = ServerFactory()
    plan = start(server)

    response = client.post(url(server), {"token": plan.token, "port": "5542"})

    assert response.status_code == 302
    assert not SpeedTestResult.objects.exists()


def test_summary_averages_the_tests_of_each_port() -> None:
    server = ServerFactory()
    now = timezone.now()
    for test, (port, reachable, download) in enumerate(
        [(5542, True, 90.0), (5542, True, 30.0), (5542, False, None), (443, True, 10.0)]
    ):
        SpeedTestResult.objects.create(
            server=server, test=test, port=port, reachable=reachable, download=download
        )
    SpeedTestResult.objects.create(server=ServerFactory(), test=9, port=5542, download=1.0)

    rows = summary(server, now)

    assert [(row.port, row.tests, row.unreachable, row.download) for row in rows] == [
        (443, 1, 0, 10.0),
        (5542, 3, 1, 60.0),
    ]
    assert rows[1].upload is None


def test_old_results_leave_the_summary_and_are_pruned() -> None:
    server = ServerFactory()
    old = SpeedTestResult.objects.create(server=server, test=1, port=5542, download=5.0)
    SpeedTestResult.objects.filter(pk=old.pk).update(
        measured_at=timezone.now() - KEEP - timedelta(days=1)
    )
    SpeedTestResult.objects.create(server=server, test=2, port=5542, download=7.0)

    assert [row.tests for row in summary(server, timezone.now())] == [1]
    assert prune_speed_results() == 1
    assert prune(timezone.now()) == 1
    assert not SpeedTestResult.objects.exists()
