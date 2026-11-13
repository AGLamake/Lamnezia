from hmac import compare_digest

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseForbidden
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.traffic.services.metrics import TrafficMetrics, render
from apps.traffic.services.online import OnlineRule

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@require_GET
def metrics(request: HttpRequest) -> HttpResponse:
    """Prometheus scrape target; off unless `METRICS_TOKEN` is set, then needs it as a Bearer."""
    token = settings.METRICS_TOKEN
    if not token:
        raise Http404
    if not compare_digest(request.headers.get("Authorization", ""), f"Bearer {token}"):
        return HttpResponseForbidden()
    body = render(TrafficMetrics(timezone.now(), OnlineRule.from_settings()).collect())
    return HttpResponse(body, content_type=_CONTENT_TYPE)
