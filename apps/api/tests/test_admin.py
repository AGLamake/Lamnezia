from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.api.models import ApiToken
from apps.api.services.tokens import PREFIX, ApiTokens
from apps.audit.models import AuditEvent

pytestmark = pytest.mark.django_db

URL = reverse("api-tokens:list")


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


def signed_in(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.parametrize("role", [Role.MODERATOR, Role.USER])
def test_only_an_administrator_opens_the_page(role: str) -> None:
    response = signed_in(UserFactory(role=role)).get(URL)

    assert response.status_code in (302, 403)


def test_a_new_token_is_shown_once(admin: User) -> None:
    moderator = UserFactory(role=Role.MODERATOR)
    client = signed_in(admin)

    response = client.post(URL, {"name": "monitoring", "owner": moderator.pk, "scope": "read"})

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    value = response.context["issued"].value
    assert value.startswith(PREFIX)
    assert value in response.content.decode()
    token = ApiToken.objects.get()
    assert (token.owner, token.created_by, token.name) == (moderator, admin, "monitoring")
    assert value not in client.get(URL).content.decode()
    assert AuditEvent.objects.filter(action="api.token_create").exists()


def test_a_token_acts_only_as_staff(admin: User) -> None:
    user = UserFactory(role=Role.USER)

    response = signed_in(admin).post(URL, {"name": "x", "owner": user.pk, "scope": "read"})

    assert response.status_code == 200
    assert "owner" in response.context["form"].errors
    assert not ApiToken.objects.exists()


def test_an_expiry_in_the_past_is_refused(admin: User) -> None:
    past = (timezone.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")

    response = signed_in(admin).post(
        URL, {"name": "x", "owner": admin.pk, "scope": "read", "expires_at": past}
    )

    assert "expires_at" in response.context["form"].errors


def test_a_token_is_disabled(admin: User) -> None:
    token = ApiTokens().issue(name="old", owner=admin, by=admin).token

    response = signed_in(admin).post(reverse("api-tokens:deactivate", args=[token.pk]))

    assert response.status_code == 302
    assert not ApiToken.objects.get(pk=token.pk).is_active
    assert AuditEvent.objects.filter(action="api.token_deactivate").count() == 1


def test_a_moderator_cannot_disable_a_token(admin: User) -> None:
    token = ApiTokens().issue(name="old", owner=admin, by=admin).token

    signed_in(UserFactory(role=Role.MODERATOR)).post(
        reverse("api-tokens:deactivate", args=[token.pk])
    )

    assert ApiToken.objects.get(pk=token.pk).is_active
