"""Second sign-in factor: one TOTP authenticator app per account and its backup codes."""

from base64 import b32encode
from dataclasses import dataclass

import segno
from django.conf import settings
from django.db import transaction
from django_otp import verify_token
from django_otp.models import Device
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import User
from apps.accounts.services.backup_codes import BackupCodes, looks_like_backup_code

_QR_SCALE = 5


@dataclass(frozen=True)
class Enrollment:
    """What the user enters into the authenticator app: a QR code or the secret by hand."""

    secret: str
    qr_data_uri: str


class TwoFactor:
    """The user's authenticator: setting it up, checking a code, removing it."""

    def __init__(self, user: User) -> None:
        self._user = user

    @property
    def enabled(self) -> bool:
        return bool(TOTPDevice.objects.filter(user=self._user, confirmed=True).exists())

    @property
    def required(self) -> bool:
        return self._user.is_admin and settings.REQUIRE_ADMIN_2FA

    def enrollment(self) -> Enrollment:
        device = self._pending_device()
        return Enrollment(
            secret=b32encode(device.bin_key).decode(),
            qr_data_uri=segno.make(device.config_url, error="m").svg_data_uri(scale=_QR_SCALE),
        )

    def confirm(self, token: str) -> TOTPDevice | None:
        """Turn on the device being set up if `token` matches; it replaces the previous one."""
        device = self._pending_device()
        if verify_token(self._user, device.persistent_id, token) is None:
            return None
        with transaction.atomic():
            TOTPDevice.objects.filter(user=self._user).exclude(pk=device.pk).delete()
            device.confirmed = True
            device.save(update_fields=["confirmed"])
        return device

    def verify(self, token: str) -> TOTPDevice | None:
        """The confirmed device if `token` matches it; repeated failures are throttled."""
        device = TOTPDevice.objects.filter(user=self._user, confirmed=True).first()
        if device is None:
            return None
        verified: TOTPDevice | None = verify_token(self._user, device.persistent_id, token)
        return verified

    def sign_in(self, token: str) -> Device | None:
        """The device that `token` matches: the authenticator, or the backup codes if it is one."""
        if looks_like_backup_code(token):
            return BackupCodes(self._user).use(token)
        return self.verify(token)

    def disable(self) -> None:
        """Removes the authenticator and the backup codes with it."""
        with transaction.atomic():
            TOTPDevice.objects.filter(user=self._user).delete()
            BackupCodes(self._user).revoke()

    def _pending_device(self) -> TOTPDevice:
        # Kept between page loads so the QR code stays the same until the first code confirms it.
        device = TOTPDevice.objects.filter(user=self._user, confirmed=False).first()
        return device or TOTPDevice.objects.create(user=self._user, name="default", confirmed=False)
