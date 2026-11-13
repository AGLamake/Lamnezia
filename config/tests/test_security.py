import pytest
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory, override_settings

from apps.audit.services.journal import client_ip
from config.security import TrustedProxyMiddleware

FORWARDED = {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"}


def passed_on(remote_addr: str) -> HttpRequest:
    """The request as the views see it after the middleware."""
    seen: list[HttpRequest] = []

    def view(request: HttpRequest) -> HttpResponse:
        seen.append(request)
        return HttpResponse()

    request = RequestFactory().get("/", REMOTE_ADDR=remote_addr, headers=FORWARDED)
    TrustedProxyMiddleware(view)(request)
    return seen[0]


def test_headers_from_the_proxy_on_this_host_are_kept() -> None:
    request = passed_on("172.18.0.1")

    assert client_ip(request) == "203.0.113.7"
    assert request.is_secure()


def test_forged_headers_from_an_untrusted_address_are_dropped() -> None:
    request = passed_on("198.51.100.20")

    assert client_ip(request) == "198.51.100.20"
    assert not request.is_secure()


@override_settings(TRUSTED_PROXIES=["192.0.2.10"])
def test_only_the_configured_proxies_are_trusted() -> None:
    assert client_ip(passed_on("192.0.2.10")) == "203.0.113.7"
    assert client_ip(passed_on("172.18.0.1")) == "172.18.0.1"


@override_settings(TRUSTED_PROXIES=["not-a-network"])
def test_a_wrong_proxy_address_stops_the_start() -> None:
    with pytest.raises(ImproperlyConfigured, match="LAMNEZIA_TRUSTED_PROXIES"):
        TrustedProxyMiddleware(HttpResponse)


@pytest.mark.django_db
def test_pages_have_a_content_security_policy(client: Client) -> None:
    policy = client.get("/accounts/login/")["Content-Security-Policy"]

    assert "default-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy


@pytest.mark.django_db
def test_https_responses_have_hsts(client: Client) -> None:
    response = client.get("/accounts/login/", secure=True)

    assert response["Strict-Transport-Security"] == "max-age=31536000"


@pytest.mark.django_db
def test_csrf_cookie_is_not_readable_by_scripts(client: Client) -> None:
    response = client.get("/accounts/login/")

    assert response.cookies["csrftoken"]["httponly"]
