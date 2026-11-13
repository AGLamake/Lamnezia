"""Fernet encryption of secrets stored in the database (SSH credentials, private keys)."""

from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


class SecretCipher:
    """Encrypts and decrypts text with the keys from LAMNEZIA_ENCRYPTION_KEY.

    The setting holds one key or several separated by commas: the first one encrypts, all of them
    decrypt, so a new key can be put in front of the old one and the secrets re-encrypted with
    ``manage.py rotate_encryption_key``.
    """

    def __init__(self, keys: str) -> None:
        try:
            self._fernet = MultiFernet([Fernet(key.strip()) for key in keys.split(",")])
        except ValueError as exc:
            raise ImproperlyConfigured(
                "LAMNEZIA_ENCRYPTION_KEY must hold Fernet keys separated by commas: "
                "32 url-safe base64-encoded bytes each."
            ) from exc

    @classmethod
    def from_settings(cls) -> "SecretCipher":
        return cls(settings.ENCRYPTION_KEY)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def rotate(self, token: str) -> str:
        """The same secret encrypted with the first key."""
        try:
            return self._fernet.rotate(token.encode()).decode()
        except InvalidToken as exc:
            raise ImproperlyConfigured(
                "A stored secret cannot be decrypted with LAMNEZIA_ENCRYPTION_KEY."
            ) from exc

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            # Wrong or rotated key: failing loudly is better than handing out garbage.
            raise ImproperlyConfigured(
                "A stored secret cannot be decrypted with LAMNEZIA_ENCRYPTION_KEY."
            ) from exc


class EncryptedTextField(models.TextField):  # type: ignore[type-arg]
    """Text field stored as a Fernet token; Python code sees plain text."""

    def from_db_value(self, value: str | None, expression: Any, connection: Any) -> str | None:
        if value is None:
            return None
        return SecretCipher.from_settings().decrypt(value)

    def get_prep_value(self, value: Any) -> str | None:
        value = super().get_prep_value(value)
        if value is None:
            return None
        return SecretCipher.from_settings().encrypt(value)


def encrypted_models() -> list[tuple[type[models.Model], list[str]]]:
    """Every model with encrypted fields, and the names of those fields."""
    found = []
    for model in apps.get_models():
        fields = [f.name for f in model._meta.concrete_fields if isinstance(f, EncryptedTextField)]
        if fields:
            found.append((model, fields))
    return found
