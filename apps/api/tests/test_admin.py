from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.api.models import ApiToken, TokenPermission
from apps.api.services.tokens import PREFIX, ApiTokens
from apps.audit.models import AuditEvent
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

URL = reverse("api-tokens:list")


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


def signed_in(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def form(name: str = "monitoring", *rows: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """The POST of the page: the token fields and permission rows, four empty ones by default
    (rows the page added)."""
    data: dict[str, Any] = {
        "name": name,
        "permissions-TOTAL_FORMS": max(len(rows), 4),
        "permissions-INITIAL_FORMS": 0,
        **extra,
    }
    for index, row in enumerate(rows):
        for field, value in row.items():
            data[f"permissions-{index}-{field}"] = value
    for index in range(max(len(rows), 4)):
        data.setdefault(f"permissions-{index}-action", "read")
    return data


def test_a_user_cannot_open_the_page() -> None:
    response = signed_in(UserFactory(role=Role.USER)).get(URL)

    assert response.status_code in (302, 403)


def test_a_moderator_issues_their_own_token() -> None:
    moderator = UserFactory(role=Role.MODERATOR)
    client = signed_in(moderator)

    response = client.post(URL, form("billing", {"resource": "keys", "action": "manage"}))

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    value = response.context["issued"].value
    assert value.startswith(PREFIX)
    assert value in response.content.decode()
    token = ApiToken.objects.get()
    assert (token.owner, token.created_by, token.name) == (moderator, moderator, "billing")
    assert list(token.permissions.values_list("resource", "action")) == [("keys", "manage")]
    assert value not in client.get(URL).content.decode()
    assert AuditEvent.objects.filter(action="api.token_create").exists()


def test_the_builder_offers_only_servers_of_the_zone() -> None:
    moderator = UserFactory(role=Role.MODERATOR)
    mine, other = ServerFactory(), ServerFactory()
    moderator.moderated_servers.add(mine)

    response = signed_in(moderator).get(URL)

    offered = response.context["permissions"].forms[0].fields["server"].queryset
    assert list(offered) == [mine]
    refused = signed_in(moderator).post(URL, form("x", {"resource": "servers", "server": other.pk}))
    assert refused.context["issued"] is None
    assert not ApiToken.objects.exists()


def test_the_builder_starts_with_one_row_and_adds_more_on_the_page(admin: User) -> None:
    page = signed_in(admin).get(URL).content.decode()

    assert 'name="permissions-TOTAL_FORMS" value="1"' in page
    assert 'name="permissions-__prefix__-resource"' in page
    assert "Add a row" in page
    rows = [{"resource": ""}] * 5 + [{"resource": "servers"}, {"resource": "traffic"}]
    signed_in(admin).post(URL, form("added", *rows))
    resources = ApiToken.objects.get().permissions.values_list("resource", flat=True)
    assert sorted(resources) == ["servers", "traffic"]


def test_a_token_links_to_its_events_in_the_log(admin: User) -> None:
    moderator = UserFactory(role=Role.MODERATOR)
    issued = ApiTokens().issue(name="x", owner=admin, by=admin, permissions=[])
    ApiTokens().issue(name="y", owner=moderator, by=moderator, permissions=[])

    link = f"/audit/?token={issued.token.prefix}"
    assert link in signed_in(admin).get(URL).content.decode()
    # A moderator does not open the log by default: no link for them.
    assert "/audit/?token=" not in signed_in(moderator).get(URL).content.decode()


def test_a_server_permission_is_kept() -> None:
    moderator = UserFactory(role=Role.MODERATOR)
    server = ServerFactory()
    moderator.moderated_servers.add(server)

    signed_in(moderator).post(URL, form("x", {"resource": "traffic", "server": server.pk}))

    permission = TokenPermission.objects.get()
    assert (permission.resource, permission.server, permission.user) == ("traffic", server, None)


@pytest.mark.parametrize(
    ("row", "error"),
    [
        ({"resource": "servers", "action": "manage"}, "action"),
        ({"resource": "users", "server": "SERVER"}, "binding"),
        ({"resource": "operations", "server": "SERVER"}, "binding"),
    ],
)
def test_a_permission_that_does_not_fit_its_resource_is_refused(
    admin: User, row: dict[str, Any], error: str
) -> None:
    server = ServerFactory()
    row = {name: server.pk if value == "SERVER" else value for name, value in row.items()}

    response = signed_in(admin).post(URL, form("x", row))

    assert response.context["permissions"].forms[0].non_field_errors().as_data()[0].code == error
    assert not ApiToken.objects.exists()


def test_a_token_needs_a_permission(admin: User) -> None:
    response = signed_in(admin).post(URL, form("x"))

    assert response.context["permissions"].non_form_errors()
    assert not ApiToken.objects.exists()


def test_an_expiry_in_the_past_is_refused(admin: User) -> None:
    past = (timezone.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")

    response = signed_in(admin).post(URL, form("x", {"resource": "servers"}, expires_at=past))

    assert "expires_at" in response.context["form"].errors


def test_a_member_sees_only_their_own_tokens(admin: User) -> None:
    moderator = UserFactory(role=Role.MODERATOR)
    ApiTokens().issue(name="admin's", owner=admin, by=admin)
    own = ApiTokens().issue(name="mine", owner=moderator, by=moderator).token

    assert list(signed_in(moderator).get(URL).context["tokens"]) == [own]
    assert len(signed_in(admin).get(URL).context["tokens"]) == 2


def test_a_token_is_disabled(admin: User) -> None:
    token = ApiTokens().issue(name="old", owner=admin, by=admin).token

    response = signed_in(admin).post(reverse("api-tokens:deactivate", args=[token.pk]))

    assert response.status_code == 302
    assert not ApiToken.objects.get(pk=token.pk).is_active
    assert AuditEvent.objects.filter(action="api.token_deactivate").count() == 1


def test_the_administrator_disables_a_moderator_token(admin: User) -> None:
    moderator = UserFactory(role=Role.MODERATOR)
    token = ApiTokens().issue(name="old", owner=moderator, by=moderator).token

    signed_in(admin).post(reverse("api-tokens:deactivate", args=[token.pk]))

    assert not ApiToken.objects.get(pk=token.pk).is_active


def test_a_moderator_cannot_disable_someone_elses_token(admin: User) -> None:
    token = ApiTokens().issue(name="old", owner=admin, by=admin).token

    response = signed_in(UserFactory(role=Role.MODERATOR)).post(
        reverse("api-tokens:deactivate", args=[token.pk])
    )

    assert response.status_code == 404
    assert ApiToken.objects.get(pk=token.pk).is_active
