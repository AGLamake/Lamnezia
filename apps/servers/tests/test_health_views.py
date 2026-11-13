import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.servers.models import Server, ServerSample
from apps.servers.tests.factories import ServerFactory
from apps.ssh.errors import ErrorCode

pytestmark = pytest.mark.django_db

GB = 1024**3


def read(server: Server, **values: object) -> ServerSample:
    sample = ServerSample.objects.create(server=server, at=timezone.now(), **values)
    server.record_sample(sample)
    return sample


def healthy(server: Server) -> ServerSample:
    return read(
        server,
        cpu_percent=12.3,
        memory_total=2 * GB,
        memory_available=GB // 2,
        disk_total=20 * GB,
        disk_free=15 * GB,
        uptime=3 * 86400,
        containers=[
            {"name": "amnezia-awg2", "state": "running"},
            {"name": "amnezia-xray", "state": "exited"},
        ],
    )


def page(role: Role, url: str, client: Client) -> str:
    client.force_login(UserFactory(role=role))
    return client.get(url, headers={"accept-language": "en"}).content.decode()


def test_dashboard_cards_show_the_latest_reading(client: Client) -> None:
    healthy(ServerFactory(name="Amsterdam"))
    read(ServerFactory(name="Berlin"), error=ErrorCode.CONNECTION_TIMEOUT)
    ServerFactory(name="Tokyo")

    html = page(Role.ADMIN, "/dashboard/", client)

    assert "Amsterdam" in html and "Berlin" in html and "Tokyo" in html
    for text in (
        "12%",
        "75%",
        "25%",
        "3\xa0days",
        "amnezia-xray",
        "The server did not answer in time.",
    ):
        assert text in html
    assert "no data yet" in html


def test_moderators_see_the_cards_of_their_servers_without_links(client: Client) -> None:
    server = ServerFactory(name="Amsterdam")
    healthy(server)
    healthy(ServerFactory(name="Berlin"))
    moderator = UserFactory(role=Role.MODERATOR)
    moderator.moderated_servers.add(server)
    client.force_login(moderator)

    html = client.get("/dashboard/", headers={"accept-language": "en"}).content.decode()

    assert "Amsterdam" in html
    assert "Berlin" not in html
    assert f"/servers/{server.pk}/" not in html


def test_server_page_shows_the_week_by_day(client: Client) -> None:
    server = ServerFactory()
    healthy(server)
    read(server, error=ErrorCode.HOST_UNREACHABLE)

    html = page(Role.ADMIN, f"/servers/{server.pk}/", client)

    assert "Availability" in html
    assert "50%" in html
    assert 'class="sparkline"' in html
    assert "Availability by day since" in html
    assert "offline" not in html  # one missed reading is not an outage yet
