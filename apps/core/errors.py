"""The CSRF failure page in the panel design (settings.CSRF_FAILURE_VIEW).

400, 403, 404 and 500 use Django's own handlers with templates/<code>.html.
"""

from django.http import HttpRequest, HttpResponse
from django.template import loader


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """Explain a rejected form without the technical reason (it stays in the log)."""
    html = loader.render_to_string("errors/csrf.html")
    return HttpResponse(html, status=403)
