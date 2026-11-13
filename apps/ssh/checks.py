"""System checks of LAMNEZIA_ENCRYPTION_KEY: the panel does not start with a key it cannot use.

`migrate`, which the web container runs before gunicorn, runs them with the database, so a wrong
key stops the start instead of failing on the first secret a page or an operation reads.
"""

from collections.abc import Sequence
from typing import Any

from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Error, Tags, register
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError

from apps.ssh.encryption import SecretCipher, encrypted_models

_HINT = (
    "Generate one with: python -c "
    "'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
)


@register(Tags.security)
def check_encryption_key(app_configs: Sequence[AppConfig] | None, **kwargs: Any) -> list[Error]:
    """The key is set and holds Fernet keys."""
    if not settings.ENCRYPTION_KEY.strip():
        return [Error("LAMNEZIA_ENCRYPTION_KEY is not set.", hint=_HINT, id="lamnezia.E001")]
    try:
        SecretCipher.from_settings()
    except ImproperlyConfigured as error:
        return [Error(str(error), hint=_HINT, id="lamnezia.E002")]
    return []


@register(Tags.security, Tags.database)
def check_stored_secrets(
    app_configs: Sequence[AppConfig] | None, databases: Sequence[str] | None = None, **kwargs: Any
) -> list[Error]:
    """The key decrypts the secrets already in the database: one row of every encrypted model."""
    if not databases or check_encryption_key(app_configs):
        return []
    for model, fields in encrypted_models():
        try:
            model._default_manager.only("pk", *fields).first()
        except ImproperlyConfigured:
            return [_wrong_key(model._meta.label)]
        except DatabaseError:
            return []  # the tables are not created yet: nothing is stored
    return []


def _wrong_key(label: str) -> Error:
    return Error(
        f"LAMNEZIA_ENCRYPTION_KEY cannot decrypt the secrets stored in {label}.",
        hint=(
            "Put back the key the database was encrypted with. To change it, list the new key "
            "first and the old one after a comma, then run manage.py rotate_encryption_key."
        ),
        id="lamnezia.E003",
    )
