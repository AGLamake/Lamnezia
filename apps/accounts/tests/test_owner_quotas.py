"""A moderator's quotas are no higher than the ones of the server administrator who made them."""

import pytest
from django.forms import modelform_factory
from django.test import Client
from django.urls import reverse

from apps.accounts.admin import AccountChangeForm
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.keys.services.zone import KeyQuota
from apps.keys.tests.test_views import issued_key

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner() -> User:
    return UserFactory(role=Role.OWNER, user_quota=3, key_quota=5)


@pytest.fixture
def moderator(owner: User) -> User:
    return UserFactory(role=Role.MODERATOR, created_by=owner)


def _edit(client: Client, member: User, **data: object) -> dict[str, list[str]]:
    response = client.post(reverse("accounts:staff-edit", args=[member.pk]), data)
    form = response.context["form"] if response.context else None
    return dict(form.errors) if form is not None else {}


def test_the_owner_gives_no_more_than_their_own_quotas(
    client: Client, owner: User, moderator: User
) -> None:
    client.force_login(owner)

    errors = _edit(client, moderator, user_quota=4, key_quota="")

    assert set(errors) == {"user_quota", "key_quota"}
    assert "At most 3" in errors["user_quota"][0]
    assert "At most 5" in errors["key_quota"][0]
    moderator.refresh_from_db()
    assert (moderator.user_quota, moderator.key_quota) == (None, None)

    assert _edit(client, moderator, user_quota=3, key_quota=2) == {}
    moderator.refresh_from_db()
    assert (moderator.user_quota, moderator.key_quota) == (3, 2)


def test_the_administrator_is_held_to_the_owners_quotas_too(
    client: Client, owner: User, moderator: User
) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))
    own = UserFactory(role=Role.MODERATOR, created_by=UserFactory(role=Role.ADMIN))

    assert set(_edit(client, moderator, user_quota=10, key_quota=1)) == {"user_quota"}
    assert _edit(client, own, user_quota=100, key_quota="") == {}


def test_an_owner_without_quotas_sets_no_ceiling(owner: User, moderator: User) -> None:
    User.objects.filter(pk=owner.pk).update(user_quota=None, key_quota=None)
    moderator.refresh_from_db()

    assert moderator.quota_error("user_quota", None) is None
    assert moderator.effective_quota("key_quota") is None


def test_a_lowered_owner_quota_cuts_the_moderators(owner: User, moderator: User) -> None:
    User.objects.filter(pk=moderator.pk).update(user_quota=3, key_quota=5)
    User.objects.filter(pk=owner.pk).update(user_quota=1, key_quota=1)
    moderator.refresh_from_db()
    user = UserFactory(managers=[moderator])
    issued_key(user=user)

    assert (moderator.effective_quota("user_quota"), moderator.effective_quota("key_quota")) == (
        1,
        1,
    )
    assert moderator.is_over_user_quota()
    assert not moderator.is_over_user_quota(user)
    assert KeyQuota(moderator).reached()


def test_the_admin_site_refuses_a_quota_above_the_owners(owner: User, moderator: User) -> None:
    form_class = modelform_factory(
        User, form=AccountChangeForm, fields=("username", "role", "user_quota", "key_quota")
    )
    data = {"username": moderator.username, "role": Role.MODERATOR}

    over = form_class({**data, "user_quota": 4, "key_quota": 5}, instance=moderator)
    within = form_class({**data, "user_quota": 3, "key_quota": 5}, instance=moderator)

    assert set(over.errors) == {"user_quota"}
    assert within.is_valid(), within.errors
