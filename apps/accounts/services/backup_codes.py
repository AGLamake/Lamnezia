"""Backup codes of the second factor: one-time codes that sign in when the phone is lost."""

from django.db import transaction
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken

from apps.accounts.models import User

BACKUP_CODE_COUNT = 10
# django-otp makes a code of five random bytes in base32: eight lowercase letters and digits.
BACKUP_CODE_LENGTH = 8
_DEVICE_NAME = "backup"


def normalized(code: str) -> str:
    """The code as stored: without spaces and dashes, in lower case."""
    return "".join(code.split()).replace("-", "").lower()


def looks_like_backup_code(code: str) -> bool:
    return len(code) == BACKUP_CODE_LENGTH and code.isalnum()


class BackupCodes:
    """The user's set of backup codes: each works once, a new set revokes the old one."""

    def __init__(self, user: User) -> None:
        self._user = user

    @property
    def left(self) -> int:
        return int(StaticToken.objects.filter(device__user=self._user).count())

    def issue(self) -> list[str]:
        """A new set of codes, shown to the user once; the previous codes stop working."""
        codes = [StaticToken.random_token() for _ in range(BACKUP_CODE_COUNT)]
        with transaction.atomic():
            self.revoke()
            device = StaticDevice.objects.create(user=self._user, name=_DEVICE_NAME)
            StaticToken.objects.bulk_create(StaticToken(device=device, token=c) for c in codes)
        return codes

    def use(self, code: str) -> StaticDevice | None:
        """The device of the codes if `code` is one of them; the code is spent."""
        device = StaticDevice.objects.filter(user=self._user, confirmed=True).first()
        if device is None or not device.verify_token(normalized(code)):
            return None
        return device

    def revoke(self) -> None:
        StaticDevice.objects.filter(user=self._user).delete()
