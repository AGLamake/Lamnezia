"""The subscription address XRay clients fetch without signing in: the token is the access.

Besides the keys the clients get the links to the portal: the keys page and support.
"""

from django.http import Http404, HttpRequest, HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.views import View

from apps.audit.services.journal import client_ip
from apps.keys.services.subscription import FeedFormat, SubscriptionFeed, SubscriptionThrottle
from apps.plans.services.limits import effective_limits
from apps.portal.services.usage import SubscriptionUserinfo


class SubscriptionFeedView(View):
    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        if not SubscriptionThrottle(client_ip(request)).allow():
            response = HttpResponse(status=429)
            response["Retry-After"] = str(SubscriptionThrottle.WINDOW_SECONDS)
            return response
        feed = SubscriptionFeed.find(token)
        if feed is None:
            raise Http404
        kind = FeedFormat.choose(
            request.GET.get("format", ""), request.headers.get("User-Agent", "")
        )
        response = HttpResponse(feed.body(kind), content_type=kind.content_type)
        for name, value in feed.headers(kind).items():
            response[name] = value
        response["profile-web-page-url"] = request.build_absolute_uri(reverse("portal:index"))
        response["support-url"] = request.build_absolute_uri(reverse("support:index"))
        limits = effective_limits(feed.owner)
        if limits.has_plan:
            userinfo = SubscriptionUserinfo.of(feed.owner, limits, timezone.now())
            response["subscription-userinfo"] = userinfo.header()
        response["Cache-Control"] = "no-store"
        return response
