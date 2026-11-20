"""The filter of Django's error page and error mails (settings.DEFAULT_EXCEPTION_REPORTER_FILTER).

Django hides settings whose names hold API, TOKEN, KEY, SECRET, PASS and the like. A proxy URL
(TELEGRAM_PROXY, GITHUB_PROXY) may hold a login and a password too, so names with PROXY are hidden
as well.
"""

import re

from django.views.debug import SafeExceptionReporterFilter


class PanelExceptionReporterFilter(SafeExceptionReporterFilter):
    """Django's filter that also hides the proxy settings."""

    hidden_settings = re.compile(
        "API|AUTH|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE|PROXY", flags=re.IGNORECASE
    )
