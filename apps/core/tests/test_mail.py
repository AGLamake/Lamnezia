from typing import Any
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.mail import EmailMessage

from apps.core.mail import SiteEmailBackend
from apps.core.models import SiteSettings
from apps.core.services.site_settings import cached_settings

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fallback_to_memory(settings: Any) -> None:
    settings.EMAIL_FALLBACK_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def save(**fields: Any) -> None:
    SiteSettings(**fields).save()
    cached_settings.invalidate()


def letter() -> EmailMessage:
    return EmailMessage("Subject", "Body", to=["user@example.com"])


def test_without_smtp_server_uses_the_env_backend() -> None:
    SiteEmailBackend().send_messages([letter()])

    assert len(mail.outbox) == 1


def test_smtp_server_from_the_settings() -> None:
    save(smtp_host="smtp.example.com", smtp_port=465, smtp_security="ssl", smtp_password="pw")

    with patch("apps.core.mail.SmtpBackend") as smtp:
        smtp.return_value.send_messages.return_value = 1
        assert SiteEmailBackend().send_messages([letter()]) == 1

    options = smtp.call_args.kwargs
    assert (options["host"], options["port"], options["use_ssl"], options["use_tls"]) == (
        "smtp.example.com",
        465,
        True,
        False,
    )
    assert options["password"] == "pw"


def test_sender_from_the_settings() -> None:
    save(from_email="VPN <vpn@example.com>")
    message = letter()

    SiteEmailBackend().send_messages([message])

    assert mail.outbox[0].from_email == "VPN <vpn@example.com>"
