import pytest

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import KeyStatus
from apps.keys.services.zone import KeyQuota
from apps.keys.tests.test_views import issued_key

pytestmark = pytest.mark.django_db


def moderator(key_quota: int | None) -> User:
    return UserFactory(role=Role.MODERATOR, key_quota=key_quota)


def test_a_key_of_a_shared_user_counts_for_each_moderator() -> None:
    tight, roomy = moderator(1), moderator(5)
    user = UserFactory(managers=[tight, roomy])
    issued_key(user=user)

    assert KeyQuota(tight).reached()
    assert not KeyQuota(roomy).reached()
    assert KeyQuota.of(user).reached()


def test_revoked_keys_and_moderators_without_a_quota_do_not_limit() -> None:
    free, tight = moderator(None), moderator(1)
    user = UserFactory(managers=[free, tight])
    issued_key(user=user, status=KeyStatus.REVOKED)

    assert not KeyQuota.of(user).reached()
    assert not KeyQuota.of(UserFactory()).reached()
