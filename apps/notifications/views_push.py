"""Notifications in the browser for the signed-in user: subscribe, unsubscribe, check and test
from the profile (`static/js/ui.js` sends the forms, `templates/pwa/sw.js` shows the pushes).
"""

from typing import cast

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, JsonResponse
from django.views import View

from apps.accounts.models import User
from apps.accounts.services.sessions import device_name
from apps.notifications.services.browser import BrowserSubscriptions, SubscriptionError
from apps.notifications.services.delivery import send_browser_test

_BAD_REQUEST = 400


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
    def post(self, request: HttpRequest) -> JsonResponse:
        if not self.subscriptions.has(self.endpoint):
            return JsonResponse({"error": "this browser is not subscribed"}, status=_BAD_REQUEST)
        device = device_name(request.headers.get("User-Agent", "")) or "browser"
        send_browser_test(cast(User, request.user), device)
        return self.answer()
