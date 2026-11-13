from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.operations.models import Operation
from apps.operations.tests.factories import FailedOperationFactory
from apps.servers.models import AuthMethod, CheckWarning, Server
from apps.servers.services.check import CHECK_KIND
from apps.servers.tests.factories import PRIVATE_KEY, ServerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def form_data(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "Amsterdam",
        "host": "203.0.113.10",
        "port": 22,
        "username": "root",
        "auth_method": AuthMethod.KEY,
        "private_key": PRIVATE_KEY,
        "passphrase": "",
        "password": "",
        "location": "",
        "paid_until": "",
        **overrides,
    }


def checks(server: Server) -> int:
    return Operation.objects.filter(kind=CHECK_KIND, params__server_id=server.pk).count()


def test_adding_a_server_saves_it_and_starts_a_check(admin_client: Client) -> None:
    response = admin_client.post("/servers/add/", form_data(passphrase="phrase"))

    server = Server.objects.get()
    assert response.status_code == 302
    assert response["Location"] == f"/servers/{server.pk}/"
    assert (server.private_key, server.passphrase) == (PRIVATE_KEY, "phrase")
    assert checks(server) == 1


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"private_key": ""}, "private_key"),
        ({"auth_method": AuthMethod.PASSWORD}, "password"),
        ({"host": "root@203.0.113.10"}, "host"),
        ({"host": "ssh://vpn.example.com"}, "host"),
        ({"port": 70000}, "port"),
    ],
)
def test_invalid_server_is_not_saved(
    admin_client: Client, overrides: dict[str, Any], field: str
) -> None:
    response = admin_client.post("/servers/add/", form_data(**overrides))

    assert response.status_code == 200
    assert field in response.context["form"].errors
    assert not Server.objects.exists()


def test_the_same_address_cannot_be_added_twice(admin_client: Client) -> None:
    ServerFactory(host="203.0.113.10", port=22)

    response = admin_client.post("/servers/add/", form_data())

    assert "A server with this host and port is already added." in response.content.decode()


def test_saved_credentials_are_never_shown(admin_client: Client) -> None:
    server = ServerFactory(passphrase="secret-phrase")

    pages = [admin_client.get(f"/servers/{server.pk}/{page}") for page in ("", "edit/")]

    for page in pages:
        html = page.content.decode()
        assert "secret-key-body" not in html
        assert "secret-phrase" not in html


def test_editing_without_new_secrets_keeps_them_and_skips_the_check(admin_client: Client) -> None:
    server = ServerFactory(host="203.0.113.10", passphrase="phrase")

    admin_client.post(f"/servers/{server.pk}/edit/", form_data(name="Paris", private_key=""))

    server.refresh_from_db()
    assert server.name == "Paris"
    assert (server.private_key, server.passphrase) == (PRIVATE_KEY, "phrase")
    assert checks(server) == 0


def test_switching_to_a_password_drops_the_key_and_checks_again(admin_client: Client) -> None:
    server = ServerFactory(host="203.0.113.10", passphrase="phrase")

    admin_client.post(
        f"/servers/{server.pk}/edit/",
        form_data(auth_method=AuthMethod.PASSWORD, private_key="", password="pw"),
    )

    server.refresh_from_db()
    assert (server.password, server.private_key, server.passphrase) == ("pw", "", "")
    assert checks(server) == 1


def test_card_shows_why_the_latest_check_failed(admin_client: Client) -> None:
    server = ServerFactory(warnings=[CheckWarning.NO_DOCKER])
    FailedOperationFactory(
        kind=CHECK_KIND,
        params={"server_id": server.pk},
        error_message="The server rejected the login.",
        error_hint="Check the user name and the password or key.",
    )

    html = admin_client.get(f"/servers/{server.pk}/").content.decode()

    assert "The server rejected the login." in html
    assert "Check the user name and the password or key." in html
    assert str(CheckWarning.NO_DOCKER.label) in html


def test_card_links_to_the_speed_test(admin_client: Client) -> None:
    server = ServerFactory()

    html = admin_client.get(f"/servers/{server.pk}/").content.decode()

    assert f'href="/protocols/speed-test/{server.pk}/"' in html


def test_check_again_starts_a_new_check(admin_client: Client) -> None:
    server = ServerFactory()

    response = admin_client.post(f"/servers/{server.pk}/check/")

    assert response.status_code == 302
    assert checks(server) == 1


@pytest.mark.parametrize("url", ["/servers/", "/servers/add/"])
def test_servers_are_for_admins_only(client: Client, url: str) -> None:
    client.force_login(UserFactory(role=Role.MODERATOR))

    assert client.get(url).status_code == 403


def test_list_shows_servers(admin_client: Client) -> None:
    ServerFactory(name="Amsterdam", location="Netherlands")

    html = admin_client.get("/servers/").content.decode()

    assert "Amsterdam" in html
    assert "Netherlands" in html


def test_list_search_keeps_matching_servers(admin_client: Client) -> None:
    ServerFactory(name="Amsterdam", location="Netherlands")
    ServerFactory(name="Berlin", location="Germany")

    response = admin_client.get("/servers/", {"q": "nether"})

    assert [server.name for server in response.context["servers"]] == ["Amsterdam"]


def test_list_says_when_the_search_found_nothing(admin_client: Client) -> None:
    ServerFactory(name="Amsterdam")

    assert "No servers found." in admin_client.get("/servers/", {"q": "zzz"}).content.decode()


def test_list_rows_carry_quick_actions(admin_client: Client) -> None:
    server = ServerFactory(name="Amsterdam")

    html = admin_client.get("/servers/").content.decode()

    assert f'action="/servers/{server.pk}/check/"' in html
    assert f'href="/servers/{server.pk}/edit/"' in html


def test_card_shows_days_until_the_vps_is_paid(admin_client: Client) -> None:
    due = ServerFactory(paid_until=timezone.localdate() + timedelta(days=5))
    late = ServerFactory(paid_until=timezone.localdate() - timedelta(days=2))

    assert "in 5 days" in admin_client.get(f"/servers/{due.pk}/").content.decode()
    assert "overdue by 2 days" in admin_client.get(f"/servers/{late.pk}/").content.decode()
