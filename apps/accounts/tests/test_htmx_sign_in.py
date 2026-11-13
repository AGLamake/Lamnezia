from urllib.parse import parse_qs, urlsplit

import pytest
from django.http import HttpResponse, HttpResponseBase
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import TOTPDeviceFactory, UserFactory

pytestmark = pytest.mark.django_db

FRAGMENT = "/support/unread/"
PAGE = "http://testserver/portal/servers/?tab=all"


def htmx(
    client: Client, path: str = FRAGMENT, current: str = PAGE, method: str = "get"
) -> HttpResponse:
    send = getattr(client, method)
    response: HttpResponse = send(path, headers={"HX-Request": "true", "HX-Current-URL": current})
    return response


def redirect_target(response: HttpResponseBase) -> tuple[str, str]:
    parts = urlsplit(response["HX-Redirect"])
    return parts.path, parse_qs(parts.query)["next"][0]


def test_signed_out_htmx_request_sends_the_page_to_sign_in(client: Client) -> None:
    response = htmx(client)

    assert response.status_code == 204
    assert not response.content
    assert redirect_target(response) == ("/accounts/login/", "/portal/servers/?tab=all")


def test_signed_out_htmx_post_too(client: Client) -> None:
    response = htmx(client, "/accounts/messengers/offer/dismiss/", method="post")

    assert redirect_target(response)[0] == "/accounts/login/"


def test_plain_request_keeps_the_redirect(client: Client) -> None:
    response = client.get(FRAGMENT)

    assert response.status_code == 302
    assert response["Location"] == f"/accounts/login/?next={FRAGMENT}"


def test_foreign_current_page_falls_back_to_home(client: Client) -> None:
    response = htmx(client, current="https://evil.example/steal")

    assert redirect_target(response)[1] == "/"


def test_missing_current_page_falls_back_to_home(client: Client) -> None:
    response = client.get(FRAGMENT, headers={"HX-Request": "true"})

    assert redirect_target(response)[1] == "/"


def test_code_prompt_is_reached_the_same_way(client: Client) -> None:
    client.force_login(TOTPDeviceFactory().user)

    response = htmx(client)

    assert redirect_target(response) == ("/accounts/2fa/verify/", "/portal/servers/?tab=all")


def test_signed_in_htmx_request_is_untouched(client: Client) -> None:
    user: User = UserFactory()
    client.force_login(user)

    response = htmx(client)

    assert response.status_code == 200
    assert "HX-Redirect" not in response


def test_other_redirects_are_untouched(client: Client) -> None:
    client.force_login(UserFactory())

    response = htmx(client, "/accounts/messengers/")

    assert response.status_code == 302
    assert "HX-Redirect" not in response
