from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.core.forms import SiteSettingsForm
from apps.core.models import SiteSettings
from apps.core.services.site_settings import cached_settings
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import Instance, instances
from apps.operations.models import Operation, OperationStatus
from apps.portal.services.keys import MyKeys
from apps.portal.services.speed_test import (
    MINUTES,
    STARTS_PER_WINDOW,
    MySpeedTest,
    ServerBusyError,
    TooManyStartsError,
    UnavailableError,
)
from apps.portal.tests.test_portal import subscriber
from apps.protocols.services import speed_test
from apps.protocols.services.speed_test import SpeedTestPlan
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

HOST = "203.0.113.7"
SOURCE = "awg:speed"
HASH = "ab" * 32


def never_connects() -> AbstractContextManager[RemoteHost]:
    raise AssertionError("the speed test page never connects to the server")


@pytest.fixture
def server() -> Iterator[Server]:
    """A server registered as the instance of the user's keys for the time of a test."""
    server = ServerFactory(host=HOST)

    def provider() -> list[Instance]:
        return [Instance(SOURCE, "Test", HOST, never_connects, server_id=server.pk)]

    instances.register(provider)
    yield server
    instances.unregister(provider)


@pytest.fixture
def user(server: Server) -> User:
    user = subscriber(max_keys=2)
    AccessKey.objects.create(
        user=user, source=SOURCE, name="phone", public_key="pk", status=KeyStatus.ACTIVE
    )
    return user


@pytest.fixture
def user_client(client: Client, user: User) -> Client:
    client.force_login(user)
    return client


@pytest.fixture(autouse=True)
def enabled() -> SiteSettings:
    site = SiteSettings.load()
    site.portal_speed_test = True
    site.portal_speed_test_ports = "5542, 55424"
    site.save()
    cached_settings.invalidate()
    return site


def my_test(user: User) -> MySpeedTest:
    return MySpeedTest(user, MyKeys(user))


def running(server: Server, user: User | None, **params: object) -> Operation:
    """A browser test of the server that started a minute ago."""
    plan = SpeedTestPlan.new((5542,), MINUTES, "browser")
    return Operation.objects.create(
        kind=speed_test.START_KIND,
        params={"server_id": server.pk, **plan.to_params(), "certificate_hash": HASH, **params},
        status=OperationStatus.SUCCEEDED,
        created_by=user,
        finished_at=timezone.now() - timedelta(minutes=1),
    )


def url(server: Server) -> str:
    return f"/portal/servers/{server.pk}/speed-test/"


def test_the_test_is_off_by_default() -> None:
    assert SiteSettings._meta.get_field("portal_speed_test").default is False


def test_a_user_tests_only_the_servers_of_their_keys(server: Server, user: User) -> None:
    other = ServerFactory(host="203.0.113.8")

    assert my_test(user).servers() == [server]
    assert my_test(user).server(other.pk) is None
    assert my_test(UserFactory()).servers() == []


def test_nothing_is_offered_while_the_setting_is_off(
    server: Server, user: User, enabled: SiteSettings
) -> None:
    enabled.portal_speed_test = False
    enabled.save()
    cached_settings.invalidate()

    assert my_test(user).servers() == []


def test_a_server_behind_another_address_is_not_offered(user: User) -> None:
    # A relay or a group sends the key elsewhere: the address of the server stays hidden.
    hidden = ServerFactory(host="198.51.100.9")

    def provider() -> list[Instance]:
        return [Instance(SOURCE, "Relay", HOST, never_connects, server_id=hidden.pk)]

    instances.register(provider)
    try:
        assert hidden not in my_test(user).servers()
    finally:
        instances.unregister(provider)


def test_ports_taken_on_the_server_are_not_offered(server: Server) -> None:
    server.containers = [{"name": "amnezia-awg2", "port": 55424, "transport": "udp"}]
    server.save()

    assert MySpeedTest.ports(server) == (5542,)


def test_start_runs_a_short_browser_test_without_the_address(server: Server, user: User) -> None:
    operation = my_test(user).start(server, (5542,), timezone.now())

    assert operation.created_by == user
    assert operation.params["mode"] == "browser"
    assert operation.params["minutes"] == MINUTES
    assert operation.params["ports"] == [5542]
    entry = AuditEvent.objects.get(action=speed_test.START_KIND)
    assert entry.ip is None
    assert "token" not in entry.data


def test_only_allowed_free_ports_are_started(server: Server, user: User) -> None:
    with pytest.raises(UnavailableError):
        my_test(user).start(server, (22,), timezone.now())
    with pytest.raises(UnavailableError):
        my_test(user).start(server, (), timezone.now())
    assert not Operation.objects.exists()


def test_one_test_of_a_server_at_a_time(server: Server, user: User) -> None:
    neighbour = subscriber(max_keys=1)
    AccessKey.objects.create(
        user=neighbour, source=SOURCE, name="laptop", public_key="pk2", status=KeyStatus.ACTIVE
    )
    my_test(user).start(server, (5542,), timezone.now())

    with pytest.raises(ServerBusyError):
        my_test(neighbour).start(server, (5542,), timezone.now())


def test_a_running_admin_test_keeps_the_server_busy(server: Server, user: User) -> None:
    running(server, UserFactory(role=Role.ADMIN))

    assert my_test(user).busy(server, timezone.now())
    assert my_test(user).running(server, timezone.now()) is None
    with pytest.raises(ServerBusyError):
        my_test(user).start(server, (5542,), timezone.now())


def test_starts_are_limited_per_user_and_server(server: Server, user: User) -> None:
    now = timezone.now()
    for _ in range(STARTS_PER_WINDOW):
        Operation.objects.create(
            kind=speed_test.START_KIND,
            params={"server_id": server.pk, "ports": [5542], "minutes": MINUTES},
            status=OperationStatus.FAILED,
            created_by=user,
        )

    assert my_test(user).next_start(server, now)
    assert my_test(user).next_start(server, now + timedelta(hours=1, minutes=1)) is None
    with pytest.raises(TooManyStartsError):
        my_test(user).start(server, (5542,), now)


def test_page_is_not_found_while_the_setting_is_off(
    user_client: Client, server: Server, enabled: SiteSettings
) -> None:
    enabled.portal_speed_test = False
    enabled.save()
    cached_settings.invalidate()

    assert user_client.get(url(server)).status_code == 404


def test_page_of_someone_elses_server_is_not_found(user_client: Client) -> None:
    other = ServerFactory(host="203.0.113.8")

    assert user_client.get(url(other)).status_code == 404
    assert user_client.post(url(other), {"ports": ["5542"]}).status_code == 404


def test_user_starts_the_test_and_waits_for_it(user_client: Client, server: Server) -> None:
    response = user_client.post(url(server), {"ports": ["5542", "55424"]})

    assert response.status_code == 302
    page = user_client.get(url(server)).content.decode()
    assert "speed-test/state/" in page
    assert user_client.get(f"{url(server)}state/").status_code == 204


def test_more_than_four_ports_are_refused(
    user_client: Client, server: Server, enabled: SiteSettings
) -> None:
    enabled.portal_speed_test_ports = "5542, 5543, 5544, 5545, 55424"
    enabled.save()
    cached_settings.invalidate()

    response = user_client.post(url(server), {"ports": ["5542", "5543", "5544", "5545", "55424"]})

    assert response.status_code == 200
    assert not Operation.objects.exists()


def test_the_page_reloads_once_the_test_runs(
    user_client: Client, server: Server, user: User
) -> None:
    running(server, user)

    response = user_client.get(f"{url(server)}state/")

    assert response.headers["HX-Refresh"] == "true"


def test_own_running_test_opens_its_ports_to_the_page(
    user_client: Client, server: Server, user: User
) -> None:
    running(server, user)

    response = user_client.get(url(server))

    assert f"connect-src 'self' https://{HOST}:5542" in response.headers["Content-Security-Policy"]
    assert "no-store" in response.headers["Cache-Control"]
    assert "data-speed-test" in response.content.decode()


def test_someone_elses_test_shows_only_that_the_server_is_busy(
    user_client: Client, server: Server
) -> None:
    operation = running(server, UserFactory(role=Role.ADMIN))

    response = user_client.get(url(server))

    assert "connect-src" not in response.headers.get("Content-Security-Policy", "")
    assert operation.params["token"] not in response.content.decode()
    assert "Another test of this server" in response.content.decode()


def test_servers_page_links_to_the_test(user_client: Client, server: Server) -> None:
    assert url(server) in user_client.get("/portal/servers/").content.decode()


def form(**data: object) -> SiteSettingsForm:
    site = SiteSettings.load()
    fields = {name: getattr(site, name) for name in SiteSettingsForm.Meta.fields}
    fields["default_plan"] = ""
    return SiteSettingsForm({**fields, **data}, instance=site)


def test_settings_keep_the_ports_tidy() -> None:
    settings_form = form(portal_speed_test=True, portal_speed_test_ports="443,8443 , 443")

    assert settings_form.is_valid(), settings_form.errors
    assert settings_form.cleaned_data["portal_speed_test_ports"] == "443, 8443"


@pytest.mark.parametrize(
    ("ports", "error"),
    [("", "Name the ports"), ("70000", "from 1 to 65535"), ("1,2,3,4,5,6,7,8,9", "eight")],
)
def test_settings_refuse_bad_ports(ports: str, error: str) -> None:
    settings_form = form(portal_speed_test=True, portal_speed_test_ports=ports)

    assert not settings_form.is_valid()
    assert error in str(settings_form.errors["portal_speed_test_ports"])
