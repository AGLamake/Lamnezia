"""Notifications in the browser for the signed-in user: subscribe, unsubscribe, check and test
from the profile (`static/js/ui.js` sends the forms, `templates/pwa/sw.js` shows the pushes and
sends the new subscription when the browser changes it).
"""

from typing import cast

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import HttpRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.accounts.models import User
from apps.accounts.services.sessions import device_name
from apps.notifications.services.browser import BrowserSubscriptions, SubscriptionError
from apps.notifications.services.delivery import send_browser_test

_BAD_REQUEST = 400
_FORBIDDEN = 403
_TOO_MANY = 429


class _PushView(LoginRequiredMixin, View):
    @property
    def subscriptions(self) -> BrowserSubscriptions:
        return BrowserSubscriptions(cast(User, self.request.user))

    @property
    def endpoint(self) -> str:
        return self.request.POST.get("endpoint", "").strip()

    def answer(self) -> JsonResponse:
        subscriptions = self.subscriptions
        return JsonResponse(
            {"subscribed": subscriptions.has(self.endpoint), "count": subscriptions.count()}
        )


class PushSubscribeView(_PushView):
    def post(self, request: HttpRequest) -> JsonResponse:
        try:
            self.subscriptions.add(
                self.endpoint,
                request.POST.get("p256dh", ""),
                request.POST.get("auth", ""),
                request.headers.get("User-Agent", ""),
            )
        except SubscriptionError as error:
            return JsonResponse({"error": str(error)}, status=_BAD_REQUEST)
        request.session[BrowserSubscriptions.SESSION_KEY] = self.endpoint
        return self.answer()


@method_decorator(csrf_exempt, name="dispatch")
class PushResubscribeView(_PushView):
    """The new subscription in place of the old one after the browser changed it
    (`pushsubscriptionchange` in the service worker), without a visit to the profile.

    A service worker has no CSRF token (the cookie is HttpOnly), so instead only a request the
    browser itself marks as sent by the panel's own pages or worker is taken: a page of another
    site cannot set `Sec-Fetch-Site`. The old subscription must be this user's.
    """

    def post(self, request: HttpRequest) -> JsonResponse:
        if request.headers.get("Sec-Fetch-Site") != "same-origin":
            return JsonResponse({"error": "not sent by the panel"}, status=_FORBIDDEN)
        old_endpoint = request.POST.get("old_endpoint", "").strip()
        try:
            self.subscriptions.replace(
                old_endpoint,
                self.endpoint,
                request.POST.get("p256dh", ""),
                request.POST.get("auth", ""),
                request.headers.get("User-Agent", ""),
            )
        except SubscriptionError as error:
            return JsonResponse({"error": str(error)}, status=_BAD_REQUEST)
        if request.session.get(BrowserSubscriptions.SESSION_KEY) == old_endpoint:
            request.session[BrowserSubscriptions.SESSION_KEY] = self.endpoint
        return self.answer()


class PushUnsubscribeView(_PushView):
    def post(self, request: HttpRequest) -> JsonResponse:
        self.subscriptions.remove(self.endpoint)
        request.session.pop(BrowserSubscriptions.SESSION_KEY, None)
        return self.answer()


class PushStateView(_PushView):
    """Whether this browser's subscription belongs to the user: after another account signed in
    with the browser it does not.
    """

    def post(self, request: HttpRequest) -> JsonResponse:
        return self.answer()


class PushTestView(_PushView):
    """A test push, at most once in `INTERVAL_SECONDS` for a user: the button could be pressed
    without end, and each press sends to every browser of the user.
    """

    INTERVAL_SECONDS = 30

    def post(self, request: HttpRequest) -> JsonResponse:
        if not self.subscriptions.has(self.endpoint):
            return JsonResponse({"error": "this browser is not subscribed"}, status=_BAD_REQUEST)
        if not cache.add(f"push-test:{request.user.pk}", True, timeout=self.INTERVAL_SECONDS):
            message = _("A test notification was sent a moment ago. Try again in half a minute.")
            return JsonResponse({"error": message}, status=_TOO_MANY)
        device = device_name(request.headers.get("User-Agent", "")) or "browser"
        send_browser_test(cast(User, request.user), device)
        return self.answer()


class PushForgetView(_PushView):
    """Turns notifications off in one browser of the profile list, this one or another."""

    def post(self, request: HttpRequest, pk: int) -> HttpResponseRedirect:
        endpoint = self.subscriptions.forget(pk)
        if endpoint is not None:
            if request.session.get(BrowserSubscriptions.SESSION_KEY) == endpoint:
                request.session.pop(BrowserSubscriptions.SESSION_KEY)
            messages.success(request, _("Notifications are off in that browser."))
        return redirect(f"{reverse('accounts:profile')}#notifications")
