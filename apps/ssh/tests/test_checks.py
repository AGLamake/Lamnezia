import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.test import override_settings

from apps.servers.tests.factories import ServerFactory
from apps.ssh.checks import check_encryption_key, check_stored_secrets


@override_settings(ENCRYPTION_KEY="")
def test_a_missing_key_is_an_error() -> None:
    [error] = check_encryption_key(None)

    assert error.id == "lamnezia.E001"
    assert "LAMNEZIA_ENCRYPTION_KEY" in error.msg


@override_settings(ENCRYPTION_KEY="not-a-fernet-key")
def test_a_malformed_key_stops_the_start() -> None:
    with pytest.raises(SystemCheckError, match=r"lamnezia\.E002"):
        call_command("check")


def test_a_valid_key_passes() -> None:
    assert check_encryption_key(None) == []


@pytest.mark.django_db
def test_a_key_that_cannot_read_the_stored_secrets_is_an_error() -> None:
    ServerFactory()

    with override_settings(ENCRYPTION_KEY=Fernet.generate_key().decode()):
        [error] = check_stored_secrets(None, databases=["default"])

    assert error.id == "lamnezia.E003"


@pytest.mark.django_db
def test_stored_secrets_are_read_only_when_the_database_is_asked_for() -> None:
    ServerFactory()

    with override_settings(ENCRYPTION_KEY=Fernet.generate_key().decode()):
        assert check_stored_secrets(None) == []


@pytest.mark.django_db
def test_the_current_key_reads_the_stored_secrets() -> None:
    ServerFactory()

    assert check_stored_secrets(None, databases=["default"]) == []
