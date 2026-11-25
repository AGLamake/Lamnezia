from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Role, Status
from apps.accounts.tests.factories import UserFactory
from apps.api.models import ApiToken
from apps.api.services.tokens import ApiTokens, TokenOwnerError, digest

pytestmark = pytest.mark.django_db


def issue(**kwargs: object) -> tuple[ApiToken, str]:
    admin = UserFactory(role=Role.ADMIN)
    options: dict[str, object] = {"name": "billing", "owner": admin, "by": admin, **kwargs}
    issued = ApiTokens().issue(**options)  # type: ignore[arg-type]
    return issued.token, issued.value


def test_only_the_digest_of_the_value_is_stored() -> None:
    token, value = issue()

    assert value.startswith("lmz_")
    assert token.digest == digest(value)
    assert value not in {token.digest, token.prefix}
    assert value.startswith(token.prefix)
    assert token.permissions.filter(action="read", server=None, user=None, key=None).count() == 5


def test_the_issued_value_does_not_leak_into_its_repr() -> None:
    admin = UserFactory(role=Role.ADMIN)
    issued = ApiTokens().issue(name="bot", owner=admin, by=admin)

    assert issued.value not in repr(issued)


def test_a_token_is_found_by_its_value() -> None:
    token, value = issue()

    assert ApiTokens().find(value) == token
    assert ApiTokens().find(value + "x") is None
    assert ApiTokens().find("") is None


def test_a_portal_user_cannot_own_a_token() -> None:
    with pytest.raises(TokenOwnerError):
        issue(owner=UserFactory(role=Role.USER))


def test_a_moderator_may_own_a_token() -> None:
    token, _value = issue(owner=UserFactory(role=Role.MODERATOR))

    assert token.works


def test_a_deactivated_token_stops_working_for_good() -> None:
    token, _value = issue()

    assert ApiTokens().deactivate(token) is True
    assert ApiTokens().deactivate(token) is False
    token.refresh_from_db()
    assert not token.is_active
    assert not token.works


def test_an_expired_token_does_not_work() -> None:
    token, _value = issue(expires_at=timezone.now() - timedelta(minutes=1))

    assert token.is_expired
    assert not token.works


@pytest.mark.parametrize(
    "change",
    [{"role": Role.USER}, {"status": Status.BLOCKED}, {"is_active": False}],
)
def test_a_token_stops_working_when_its_owner_loses_access(change: dict[str, object]) -> None:
    owner = UserFactory(role=Role.MODERATOR)
    token, _value = issue(owner=owner)
    for field, value in change.items():
        setattr(owner, field, value)
    owner.save()

    token.refresh_from_db()
    assert not token.works
