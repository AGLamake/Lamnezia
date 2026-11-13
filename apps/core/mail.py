"""Outgoing mail through the SMTP server from SiteSettings, or LAMNEZIA_EMAIL_URL without one."""

from collections.abc import Sequence
from typing import Any

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.smtp import EmailBackend as SmtpBackend

from apps.core.models import SiteSettings, SmtpSecurity
from apps.core.services.site_settings import cached_settings

# Backends that keep letters on the server: the default prints them to the log.
_LOCAL = frozenset(
    {
        "django.core.mail.backends.console.EmailBackend",
        "django.core.mail.backends.filebased.EmailBackend",
        "django.core.mail.backends.locmem.EmailBackend",
        "django.core.mail.backends.dummy.EmailBackend",
    }
)


def mail_configured() -> bool:
    """True when letters leave the server: an SMTP server in the settings or LAMNEZIA_EMAIL_URL."""
    return bool(cached_settings.get().smtp_host) or settings.EMAIL_FALLBACK_BACKEND not in _LOCAL


class SiteEmailBackend(BaseEmailBackend):
    """Picks the mail server on each send, so a change in the settings works without a restart."""

    def send_messages(self, email_messages: Sequence[EmailMessage]) -> int:
        site = cached_settings.get()
        if site.from_email:
            for message in email_messages:
                if message.from_email == settings.DEFAULT_FROM_EMAIL:
                    message.from_email = site.from_email
        sent: int = self._backend(site).send_messages(email_messages)
        return sent

    def _backend(self, site: SiteSettings) -> BaseEmailBackend:
        if not site.smtp_host:
            return get_connection(settings.EMAIL_FALLBACK_BACKEND, fail_silently=self.fail_silently)
        options: dict[str, Any] = {
            "host": site.smtp_host,
            "port": site.smtp_port,
            "username": site.smtp_username,
            "password": site.smtp_password,
            "use_tls": site.smtp_security == SmtpSecurity.STARTTLS,
            "use_ssl": site.smtp_security == SmtpSecurity.SSL,
            "timeout": settings.EMAIL_TIMEOUT,
            "fail_silently": self.fail_silently,
        }
        return SmtpBackend(**options)
