"""Middleware of signing in: the second factor and the sign-in step for HTMX requests."""

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse, HttpResponseBase, QueryDict
from django.http.response import HttpResponseRedirectBase
from django.shortcuts import resolve_url
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from apps.accounts.models import User
from apps.accounts.services.two_factor import TwoFactor

VERIFY = "accounts:two-factor-verify"
SETUP = "accounts:two-factor-setup"
# Pages open before the second factor: leaving, switching the language and the files that
# install the panel as an app (the browser fetches them without following a redirect).
_ALWAYS_OPEN = frozenset(
    {"accounts:logout", "set_language", "pwa-manifest", "pwa-worker", "pwa-offline"}
)


def session_verified(user: User) -> bool:
    """True when this session passed the second factor (django_otp's OTPMiddleware sets it)."""
    return bool(user.is_verified())  # type: ignore[attr-defined]


class HtmxSignInMiddleware:
    """Sends the whole page to the sign-in step when an HTMX request meets it.

    Without it the browser follows the redirect inside the request and HTMX swaps the sign-in
    page into a badge or a chat. Then `next` leads back to the page the user had open, not to
    the address of the fragment. Goes before every middleware that redirects to sign in.
    """

    STEPS = (settings.LOGIN_URL, VERIFY, SETUP)

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        if not request.headers.get("HX-Request") or not isinstance(
            response, HttpResponseRedirectBase
        ):
            return response
        location = urlsplit(response["Location"])
        if location.path not in {resolve_url(step) for step in self.STEPS}:
            return response
        query = QueryDict(location.query, mutable=True)
        query[REDIRECT_FIELD_NAME] = self._current_page(request)
        redirect = HttpResponse(status=204)
        redirect["HX-Redirect"] = f"{location.path}?{query.urlencode(safe='/')}"
        return redirect

    @staticmethod
    def _current_page(request: HttpRequest) -> str:
        current = request.headers.get("HX-Current-URL", "")
        if not url_has_allowed_host_and_scheme(
            current, {request.get_host()}, require_https=request.is_secure()
        ):
            return reverse("home")
        parts = urlsplit(current)
        return urlunsplit(("", "", parts.path or "/", parts.query, ""))


class TwoFactorMiddleware:
    """Holds a signed-in user at the code prompt until the session passes the second factor.

    A user with an authenticator enters a code; an administrator without one, when
    REQUIRE_ADMIN_2FA is on, sets it up first. This covers every page, the Django admin too.
    Goes after django_otp's OTPMiddleware, which marks verified sessions.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        return self.get_response(request)

    def process_view(
        self, request: HttpRequest, view: Callable[..., Any], args: Any, kwargs: Any
    ) -> HttpResponse | None:
        user = request.user
        if not isinstance(user, User) or session_verified(user):
            return None
        target = self._target(TwoFactor(user))
        view_name = request.resolver_match.view_name if request.resolver_match else ""
        if target is None or view_name in {target, *_ALWAYS_OPEN}:
            return None
        return redirect_to_login(request.get_full_path(), reverse(target))

    @staticmethod
    def _target(two_factor: TwoFactor) -> str | None:
        if two_factor.enabled:
            return VERIFY
        return SETUP if two_factor.required else None
