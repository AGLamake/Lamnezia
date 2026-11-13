import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.db import connection

from apps.ssh.encryption import EncryptedTextField, SecretCipher


def test_cipher_round_trip() -> None:
    cipher = SecretCipher(Fernet.generate_key().decode())

    token = cipher.encrypt("-----BEGIN OPENSSH PRIVATE KEY-----")

    assert "OPENSSH" not in token
    assert cipher.decrypt(token) == "-----BEGIN OPENSSH PRIVATE KEY-----"


def test_invalid_key_is_a_configuration_error() -> None:
    with pytest.raises(ImproperlyConfigured):
        SecretCipher("")


def test_token_from_another_key_is_a_configuration_error() -> None:
    token = SecretCipher(Fernet.generate_key().decode()).encrypt("secret")

    with pytest.raises(ImproperlyConfigured):
        SecretCipher(Fernet.generate_key().decode()).decrypt(token)


def test_field_stores_ciphertext_and_reads_plaintext() -> None:
    field = EncryptedTextField()

    stored = field.get_db_prep_value("ssh-password", connection)

    assert isinstance(stored, str)
    assert "ssh-password" not in stored
    assert field.from_db_value(stored, None, connection) == "ssh-password"


def test_field_keeps_null() -> None:
    field = EncryptedTextField()

    assert field.get_db_prep_value(None, connection) is None
    assert field.from_db_value(None, None, connection) is None


def test_old_keys_still_decrypt_and_the_first_one_encrypts() -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    legacy = SecretCipher(old).encrypt("secret")
    cipher = SecretCipher(f"{new}, {old}")

    assert cipher.decrypt(legacy) == "secret"
    assert SecretCipher(new).decrypt(cipher.encrypt("secret")) == "secret"


def test_one_bad_key_in_the_list_is_a_configuration_error() -> None:
    with pytest.raises(ImproperlyConfigured):
        SecretCipher(f"{Fernet.generate_key().decode()},not-a-key")
