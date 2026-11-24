"""Additional secrets of MTProxy and Telemt in operation params: encrypted, out of the journal.

The install and the settings change carry the proxy settings in their params. The additional
secrets open the proxy like its main one, so they leave the settings and travel encrypted under
EXTRA_SECRETS_TOKEN, a key of Operation.SECRET_PARAMS: the encryption key rotation re-encrypts
them, and `without_secrets` keeps them out of the audit journal.
"""

from collections.abc import Mapping
from typing import Any

from amnezia_core.proxy_options import parse_secret_list
from apps.ssh.encryption import SecretCipher

EXTRA_SECRETS_TOKEN = "additional_secrets_token"
EXTRA_SECRETS_FIELD = "additional_secrets"


def seal_extra_secrets(options: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The settings without the additional secrets, and the params entry with them encrypted.

    Settings without the field give no entry: the settings page keeps what it did not change,
    while an empty list clears the secrets.
    """
    sealed = dict(options)
    if EXTRA_SECRETS_FIELD not in sealed:
        return sealed, {}
    secrets = ",".join(sealed.pop(EXTRA_SECRETS_FIELD) or ())
    return sealed, {EXTRA_SECRETS_TOKEN: SecretCipher.from_settings().encrypt(secrets)}


def open_extra_secrets(options: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    """The settings with the additional secrets back from `params`.

    An operation started before the encryption has them in the settings in plain; it is retried
    as it was.
    """
    token = params.get(EXTRA_SECRETS_TOKEN)
    if token is None:
        return dict(options)
    secrets = parse_secret_list(SecretCipher.from_settings().decrypt(token))
    return {**options, EXTRA_SECRETS_FIELD: list(secrets)}
