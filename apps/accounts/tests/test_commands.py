import io

import pytest
from django.core.management import CommandError, call_command

from apps.accounts.models import Role, User
from apps.accounts.services.two_factor import TwoFactor
from apps.accounts.tests.factories import TOTPDeviceFactory, UserFactory

pytestmark = pytest.mark.django_db


def create_admin(password: str, *args: str) -> None:
    call_command(
        "createadmin",
        "--username",
        "boss",
        "--password-stdin",
        *args,
        stdin=io.StringIO(password + "\n"),
        stdout=io.StringIO(),
    )


def test_createadmin_makes_an_administrator() -> None:
    create_admin("a-long-passw0rd", "--email", "boss@example.com")

    user = User.objects.get(username="boss")
    assert (user.role, user.email, user.is_superuser) == (Role.ADMIN, "boss@example.com", True)
    assert user.check_password("a-long-passw0rd")


def test_createadmin_refuses_a_weak_password() -> None:
    with pytest.raises(CommandError):
        create_admin("123")
    assert not User.objects.exists()


def test_createadmin_refuses_an_existing_name() -> None:
    UserFactory(username="boss")

    with pytest.raises(CommandError):
        create_admin("a-long-passw0rd")


def test_reset_2fa() -> None:
    device = TOTPDeviceFactory()

    call_command("reset_2fa", device.user.username, stdout=io.StringIO())

    assert not TwoFactor(device.user).enabled
