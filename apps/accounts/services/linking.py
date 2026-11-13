"""Linking Telegram and Discord accounts to panel users (DESIGN.md 6.8).

The web, the bots and OAuth all go through `AccountLinks`, so the rules live in one place:
a link code works once and expires, and a messenger account belongs to one panel user.
"""

import hashlib
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import LinkedAccount, LinkToken, Messenger, NotifyVia, User
from apps.core.models import SiteSettings


class LinkError(Exception):
    """The account was not linked; the message is for the user."""


def links_by_code(site: SiteSettings, messenger: Messenger) -> bool:
    """A link code can be issued: the bot or the application of the messenger is set up."""
    return site.telegram_linking if messenger == Messenger.TELEGRAM else site.discord_oauth


def invitation(site: SiteSettings, messenger: Messenger, code: str) -> str:
    """What the user follows or sends to link: a Telegram deep link or the Discord bot command."""
    if messenger == Messenger.TELEGRAM:
        return f"https://t.me/{site.telegram_bot_username}?start={code}"
    return f"/link {code}"


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


class AccountLinks:
    """Issues link codes, links and unlinks messenger accounts."""

    def __init__(
        self,
        *,
        code_lifetime: timedelta | None = None,
        clock: Callable[[], datetime] = timezone.now,
    ) -> None:
        self._lifetime = code_lifetime or timedelta(minutes=settings.LINK_CODE_MINUTES)
        self._clock = clock

    def issue_code(self, user: User, messenger: Messenger) -> str:
        """A new single-use code for the bot; earlier codes of this user and messenger stop working.

        Fits a Telegram deep link: `t.me/<bot>?start=<code>` takes up to 64 of `A-Za-z0-9_-`.
        """
        now = self._clock()
        code = secrets.token_urlsafe(24)
        with transaction.atomic():
            LinkToken.objects.filter(expires_at__lte=now).delete()
            LinkToken.objects.filter(user=user, messenger=messenger).delete()
            LinkToken.objects.create(
                user=user,
                messenger=messenger,
                digest=_digest(code),
                expires_at=now + self._lifetime,
            )
        return code

    def redeem(
        self, messenger: Messenger, code: str, external_id: str, username: str = ""
    ) -> LinkedAccount:
        """Link the messenger account that sent `code` to the user the code was issued for."""
        token = LinkToken.objects.filter(
            messenger=messenger, digest=_digest(code), expires_at__gt=self._clock()
        ).first()
        # Deleting the row is what spends the code: of two concurrent redeems only one deletes it.
        if token is None or not LinkToken.objects.filter(pk=token.pk).delete()[0]:
            raise LinkError(_("The code is wrong or expired. Get a new one in your profile."))
        return self.link(token.user, messenger, external_id, username)

    def link(
        self, user: User, messenger: Messenger, external_id: str, username: str = ""
    ) -> LinkedAccount:
        """Link `external_id` to `user`, replacing their previous account of this messenger."""
        taken = LinkedAccount.objects.filter(messenger=messenger, external_id=external_id)
        if taken.exclude(user=user).exists():
            raise LinkError(_("This account is already linked to another user of the panel."))
        try:
            with transaction.atomic():
                account, _created = LinkedAccount.objects.update_or_create(
                    user=user,
                    messenger=messenger,
                    defaults={"external_id": external_id, "username": username},
                )
        except IntegrityError:
            # Another user linked the same account between the check and the write.
            raise LinkError(
                _("This account is already linked to another user of the panel.")
            ) from None
        return account

    def unlink(self, user: User, messenger: Messenger) -> bool:
        """Remove the user's link; notifications that went there go to email again."""
        with transaction.atomic():
            deleted = LinkedAccount.objects.filter(user=user, messenger=messenger).delete()[0]
            User.objects.filter(pk=user.pk, notify_via=messenger).update(notify_via=NotifyVia.EMAIL)
        user.refresh_from_db(fields=["notify_via"])
        return bool(deleted)

    @staticmethod
    def user_of(messenger: Messenger, external_id: str) -> User | None:
        """The active panel user this messenger account is linked to."""
        account = (
            LinkedAccount.objects.select_related("user")
            .filter(messenger=messenger, external_id=external_id, user__is_active=True)
            .first()
        )
        return account.user if account else None
