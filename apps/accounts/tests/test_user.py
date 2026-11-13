import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def test_auth_user_model_is_custom_user() -> None:
    assert get_user_model() is User


def test_factory_creates_user_with_working_password() -> None:
    user = UserFactory()

    assert User.objects.get(pk=user.pk).check_password("password")


def test_new_user_is_a_client() -> None:
    user = UserFactory()

    assert user.role == Role.USER
    assert not (user.is_staff_member or user.is_staff or user.is_superuser)


@pytest.mark.parametrize(
    ("role", "staff_member", "django_admin"),
    [(Role.ADMIN, True, True), (Role.MODERATOR, True, False), (Role.USER, False, False)],
)
def test_role_sets_access(role: Role, staff_member: bool, django_admin: bool) -> None:
    user = UserFactory(role=role, is_staff=True, is_superuser=True)

    assert user.is_staff_member is staff_member
    assert (user.is_staff, user.is_superuser) == (django_admin, django_admin)


def test_superuser_is_an_admin() -> None:
    user = User.objects.create_superuser("root", "root@example.com", "password")

    assert user.role == Role.ADMIN
    assert user.is_admin


class TestModerator:
    def test_the_user_quota_stops_new_users_only(self) -> None:
        moderator = UserFactory(role=Role.MODERATOR, user_quota=1)
        served = UserFactory(manager=moderator)
        newcomer = UserFactory()
        newcomer.manager = moderator

        served.full_clean()
        with pytest.raises(ValidationError, match="as many users as allowed"):
            newcomer.full_clean()

    def test_no_quota_means_no_limit(self) -> None:
        moderator = UserFactory(role=Role.MODERATOR)
        UserFactory.create_batch(3, manager=moderator)

        UserFactory.build(manager=moderator).full_clean(exclude=["password"])

    def test_nobody_is_their_own_moderator(self) -> None:
        moderator = UserFactory(role=Role.MODERATOR)
        moderator.manager = moderator

        with pytest.raises(ValidationError, match="their own moderator"):
            moderator.full_clean()
