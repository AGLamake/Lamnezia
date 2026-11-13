import re
from typing import Any

import pytest
from django.core import mail
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.letters import html_version

pytestmark = pytest.mark.django_db

LOGIN = "/accounts/login/"


def log_in(client: Client, username: str, password: str, ip: str = "203.0.113.5") -> int:
    response = client.post(LOGIN, {"username": username, "password": password}, REMOTE_ADDR=ip)
    return response.status_code


def test_login_leads_to_the_start_page(client: Client) -> None:
    user = UserFactory()

    response = client.post(LOGIN, {"username": user.username, "password": "password"})

    assert response["Location"] == "/"
    assert client.session["_auth_user_id"] == str(user.pk)


def test_login_page_is_shown(client: Client) -> None:
    assert client.get(LOGIN).status_code == 200


def test_admin_site_signs_in_through_the_panel(client: Client) -> None:
    response = client.get("/admin/login/?next=/admin/")

    assert response["Location"] == f"{LOGIN}?next=/admin/"


class TestBruteForce:
    @pytest.fixture(autouse=True)
    def _low_limit(self, settings: Any) -> None:
        settings.AXES_FAILURE_LIMIT = 3

    def test_account_is_locked_after_the_limit(self, client: Client) -> None:
        user = UserFactory()
        for _ in range(3):
            log_in(client, user.username, "wrong")

        assert log_in(client, user.username, "password") == 429
        assert "_auth_user_id" not in client.session

    def test_lockout_page_says_when_to_try_again(self, client: Client) -> None:
        user = UserFactory()
        for _ in range(3):
            log_in(client, user.username, "wrong")

        html = client.post(
            LOGIN, {"username": user.username, "password": "x"}, REMOTE_ADDR="203.0.113.5"
        ).content.decode()

        assert "15" in html

    def test_lockout_is_per_address(self, client: Client) -> None:
        user = UserFactory()
        for _ in range(3):
            log_in(client, user.username, "wrong")

        assert log_in(client, user.username, "password", ip="198.51.100.7") == 302

    def test_address_comes_from_the_header_nginx_appends(self, client: Client) -> None:
        user = UserFactory()
        for _ in range(3):
            client.post(
                LOGIN,
                {"username": user.username, "password": "wrong"},
                headers={"x-forwarded-for": "1.1.1.1, 203.0.113.9"},
            )

        # A forged first entry does not help: the last one is still the locked address.
        response = client.post(
            LOGIN,
            {"username": user.username, "password": "password"},
            headers={"x-forwarded-for": "8.8.8.8, 203.0.113.9"},
        )
        assert response.status_code == 429

    def test_successful_login_resets_the_counter(self, client: Client) -> None:
        user = UserFactory()
        log_in(client, user.username, "wrong")
        log_in(client, user.username, "wrong")
        log_in(client, user.username, "password")
        client.logout()

        assert log_in(client, user.username, "wrong") == 200
        assert log_in(client, user.username, "password") == 302


def test_password_reset_letter_has_an_html_version(client: Client) -> None:
    user = UserFactory()

    client.post("/accounts/password/reset/", {"email": user.email})

    html = html_version(mail.outbox[0])
    assert re.search(r'href="https?://[^/]+/accounts/password/reset/[^/"]+/[^/"]+/"', html)


def test_sign_in_forms_tell_password_managers_what_to_fill(client: Client) -> None:
    login = client.get("/accounts/login/").content.decode()
    reset = client.get("/accounts/password/reset/").content.decode()

    assert 'autocomplete="username"' in login
    assert 'autocomplete="current-password"' in login
    assert 'autocomplete="email"' in reset


def test_password_reset_by_email(client: Client) -> None:
    user = UserFactory()

    client.post("/accounts/password/reset/", {"email": user.email})
    link = re.search(r"https?://[^/]+(/accounts/password/reset/\S+)", str(mail.outbox[0].body))
    assert link is not None
    form_url = client.get(link.group(1))["Location"]
    response = client.post(
        form_url, {"new_password1": "a-new-long-passw0rd", "new_password2": "a-new-long-passw0rd"}
    )

    assert response["Location"] == LOGIN
    user.refresh_from_db()
    assert user.check_password("a-new-long-passw0rd")


def test_password_change(client: Client) -> None:
    user = UserFactory()
    client.force_login(user)

    response = client.post(
        "/accounts/password/",
        {
            "old_password": "password",
            "new_password1": "a-new-long-passw0rd",
            "new_password2": "a-new-long-passw0rd",
        },
    )

    assert response["Location"] == "/accounts/profile/#password"
    user.refresh_from_db()
    assert user.check_password("a-new-long-passw0rd")


@pytest.mark.parametrize(
    "page",
    [
        "/accounts/profile/",
        "/accounts/password/reset/",
        "/accounts/password/reset/sent/",
        "/accounts/password/reset/bad/link/",
        "/accounts/2fa/setup/",
        "/accounts/2fa/disable/",
    ],
)
def test_account_pages_render(client: Client, page: str) -> None:
    client.force_login(UserFactory())

    assert client.get(page).status_code == 200
