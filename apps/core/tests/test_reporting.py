import sys
from typing import Any

from django.test import RequestFactory
from django.views.debug import technical_500_response

# Built at run time: the page also quotes the source lines around the error.
PASSWORD = "-".join(("proxy", "secret", "42"))


def error_page() -> str:
    request = RequestFactory().get("/")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        response = technical_500_response(request, *sys.exc_info())
    return response.content.decode()


def test_debug_error_page_hides_the_proxy_password(settings: Any) -> None:
    settings.DEBUG = True
    settings.TELEGRAM_PROXY = f"socks5://user:{PASSWORD}@proxy.example:1080"
    settings.GITHUB_PROXY = f"http://user:{PASSWORD}@proxy.example:3128"

    page = error_page()

    assert "TELEGRAM_PROXY" in page
    assert "GITHUB_PROXY" in page
    assert PASSWORD not in page
