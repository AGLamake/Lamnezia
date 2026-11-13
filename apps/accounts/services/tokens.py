"""Single-use links in letters: confirming an email and signing in without a password."""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core import signing

from apps.accounts.models import User


class EmailConfirmationTokens(PasswordResetTokenGenerator):
    """Valid until the account is active, so a link confirms once. Lives PASSWORD_RESET_TIMEOUT."""

    key_salt = "apps.accounts.email_confirmation"

    def _make_hash_value(self, user: User, timestamp: int) -> str:
        return f"{user.pk}{user.is_active}{user.email}{timestamp}"


email_confirmation = EmailConfirmationTokens()


class MagicLink:
    """A signed sign-in token for active users; it stops working at the next login."""

    _SALT = "apps.accounts.magic_link"

    def make(self, user: User) -> str:
        return signing.dumps([user.pk, _login_stamp(user)], salt=self._SALT)

    def user(self, token: str) -> User | None:
        try:
            pk, stamp = signing.loads(
                token, salt=self._SALT, max_age=timedelta(minutes=settings.MAGIC_LINK_MINUTES)
            )
        except (signing.BadSignature, ValueError):
            return None
        user = User.objects.filter(pk=pk, is_active=True).first()
        return user if user is not None and _login_stamp(user) == stamp else None


magic_link = MagicLink()


def _login_stamp(user: User) -> str:
    return user.last_login.isoformat() if user.last_login else ""


class EmailChange:
    """A signed link to the new address: it sets the address while the old one is still in place."""

    _SALT = "apps.accounts.email_change"

    def make(self, user: User, new_email: str) -> str:
        return signing.dumps([user.pk, new_email, user.email], salt=self._SALT)

    def apply(self, token: str, user: User) -> str | None:
        """Set the address of the link on `user`; the address, or None for a stale link."""
        try:
            pk, new_email, old_email = signing.loads(
                token, salt=self._SALT, max_age=settings.PASSWORD_RESET_TIMEOUT
            )
        except (signing.BadSignature, ValueError):
            return None
        taken = User.objects.filter(email__iexact=new_email).exclude(pk=user.pk).exists()
        if pk != user.pk or user.email != old_email or taken:
            return None
        user.email = new_email
        user.save(update_fields=["email"])
        return str(new_email)


email_change = EmailChange()
