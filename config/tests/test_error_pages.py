"""Error pages in the panel design: 400, 403, 404, 500 and the CSRF failure."""

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory, override_settings
from django.urls import path
from django.views import defaults

from apps.core.errors import csrf_failure


def broken(request: HttpRequest) -> HttpResponse:
    raise RuntimeError("boom")


# The error pages link to "home", so the test URLconf has one too.
urlpatterns = [path("broken/", broken), path("", broken, name="home")]


@pytest.mark.django_db
@override_settings(DEBUG=False)
def test_missing_page_shows_the_404_page(client: Client) -> None:
    response = client.get("/no-such-page/")

    assert response.status_code == 404
    html = response.content.decode()
    assert "Page not found" in html
    assert 'href="/"' in html
    assert "css/app.css" in html


@pytest.mark.parametrize(
    ("view", "status", "text"),
    [
        (defaults.bad_request, 400, "Bad request"),
        (defaults.permission_denied, 403, "Access denied"),
    ],
)
@pytest.mark.django_db
def test_client_errors_have_their_pages(view: object, status: int, text: str) -> None:
    request = RequestFactory().get("/")

    response = view(request, exception=Exception())  # type: ignore[operator]

    assert response.status_code == status
    assert text in response.content.decode()


def test_server_error_page_needs_neither_the_database_nor_the_request() -> None:
    # No django_db mark: a query here would fail the test.
    response = defaults.server_error(RequestFactory().get("/"))

    assert response.status_code == 500
    html = response.content.decode()
    assert "Something went wrong" in html
    assert 'href="/"' in html


@override_settings(DEBUG=False, ROOT_URLCONF=__name__)
def test_unhandled_exception_renders_the_500_page() -> None:
    client = Client(raise_request_exception=False)

    response = client.get("/broken/")

    assert response.status_code == 500
    assert "Something went wrong" in response.content.decode()


def test_csrf_failure_explains_what_to_do() -> None:
    response = csrf_failure(RequestFactory().post("/"), reason="CSRF token missing.")

    assert response.status_code == 403
    html = response.content.decode()
    assert "The form is out of date" in html
    assert "CSRF token missing" not in html


@pytest.mark.django_db
def test_rejected_post_uses_the_csrf_page() -> None:
    client = Client(enforce_csrf_checks=True)

    response = client.post("/accounts/login/", {"username": "x", "password": "y"})

    assert response.status_code == 403
    assert "The form is out of date" in response.content.decode()
