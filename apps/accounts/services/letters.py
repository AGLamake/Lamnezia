"""Letters of registration and sign-in. They go out from the worker after the transaction."""

from typing import Any

from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.models import Role, User
from apps.accounts.services.link_offer import linkable, named
from apps.accounts.services.tokens import email_change, email_confirmation, magic_link
from apps.accounts.tasks import send_letter
from apps.core.services.site_settings import cached_settings


class Letters:
    """Builds the links on `base_url`, the panel address the visitor came by."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def confirm_email(self, user: User) -> None:
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        link = self._url("accounts:confirm-email", uid, email_confirmation.make_token(user))
        messengers = named(linkable(cached_settings.get()))
        profile = self._url("accounts:linked")
        self._send(
            "confirm_email",
            [user.email],
            user=user,
            link=link,
            messengers=messengers,
            profile=profile,
        )

    def magic_link(self, user: User) -> None:
        link = self._url("accounts:magic-login", magic_link.make(user))
        self._send("magic_link", [user.email], user=user, link=link)

    def change_email(self, user: User, new_email: str) -> None:
        """Ask the new address to confirm itself; the account keeps the old one until then."""
        link = self._url("accounts:email-confirm", email_change.make(user, new_email))
        self._send("change_email", [new_email], user=user, link=link)

    def new_application(self, user: User) -> None:
        """Tell the administrators that a new user waits for a plan."""
        admins = User.objects.filter(role=Role.ADMIN, is_active=True).exclude(email="")
        recipients = list(admins.values_list("email", flat=True))
        if recipients:
            link = self._url("accounts:registrations")
            self._send("new_application", recipients, user=user, link=link)

    def _url(self, name: str, *args: str) -> str:
        return self._base_url + reverse(name, args=args)

    @staticmethod
    def _send(template: str, to: list[str], **context: Any) -> None:
        context["site_name"] = cached_settings.get().site_name
        subject = render_to_string(f"accounts/email/{template}_subject.txt", context).strip()
        body = render_to_string(f"accounts/email/{template}.txt", context)
        html = render_to_string(f"accounts/email/{template}.html", context)
        transaction.on_commit(lambda: send_letter.delay(subject, body, to, html))
