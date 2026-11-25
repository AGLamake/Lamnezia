"""Permissions of tokens on the API: resources, objects, the owner's zone and changes."""

import importlib
import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.apps import apps as django_apps
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.api.models import ApiToken, TokenPermission
from apps.api.services.permissions import PermissionRuleError
from apps.api.services.tokens import (
    ApiTokens,
    IssuedToken,
    Permission,
    PermissionZoneError,
    digest,
)
from apps.api.tests.test_views import body, call, make_key, moderated
from apps.audit.models import AuditEvent
from apps.backups.tests.helpers import awg_instance
from apps.keys.models import AccessKey, KeyStatus
from apps.operations.models import Operation
from apps.plans.models import UserLimits
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


def issue(owner: User, *permissions: Permission) -> IssuedToken:
    return ApiTokens().issue(name="integration", owner=owner, by=owner, permissions=permissions)


def post(path: str, issued: IssuedToken, data: Any = None) -> Any:
    return Client().post(
        f"/api/v1/{path}",
        json.dumps(data or {}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {issued.value}",
    )


def ids(response: Any) -> list[int]:
    return [row["id"] for row in body(response)["results"]]


class TestResources:
    def test_a_call_without_a_permission_for_the_resource_is_forbidden(self, admin: User) -> None:
        token = issue(admin, Permission("servers"))

        assert call("servers", token).status_code == 200
        refused = call("keys", token)
        assert refused.status_code == 403
        assert body(refused)["error"]["code"] == "forbidden"
        assert call("traffic", token).status_code == 403

    def test_the_token_endpoint_describes_the_permissions(self, admin: User) -> None:
        server = ServerFactory()
        token = issue(admin, Permission("keys", "manage", server=server))

        data = body(call("token", token))

        assert data["scope"] == "manage"
        assert data["permissions"] == [
            {
                "resource": "keys",
                "action": "manage",
                "server_id": server.pk,
                "user_id": None,
                "key_id": None,
            }
        ]

    def test_a_permission_must_fit_its_resource(self, admin: User) -> None:
        with pytest.raises(PermissionRuleError):
            issue(admin, Permission("traffic", "manage"))
        with pytest.raises(PermissionRuleError):
            issue(admin, Permission("servers", user=admin))
        assert not ApiToken.objects.exists()


class TestObjects:
    def test_a_server_permission_shows_only_that_server(self, admin: User) -> None:
        mine, other = ServerFactory(), ServerFactory()
        token = issue(admin, Permission("servers", server=mine))

        assert ids(call("servers", token)) == [mine.pk]
        assert call(f"servers/{mine.pk}", token).status_code == 200
        assert call(f"servers/{other.pk}", token).status_code == 404

    def test_a_key_permission_shows_only_that_key(self, admin: User) -> None:
        instance = awg_instance()
        user = UserFactory()
        mine, other = make_key(instance, user), make_key(instance, user, "laptop")
        token = issue(admin, Permission("keys", key=mine))

        assert ids(call("keys", token)) == [mine.pk]
        assert call(f"keys/{other.pk}", token).status_code == 404

    def test_a_server_permission_on_keys_covers_the_keys_on_it(self, admin: User) -> None:
        here, there = awg_instance(), awg_instance()
        user = UserFactory()
        mine, other = make_key(here, user), make_key(there, user, "laptop")
        token = issue(admin, Permission("keys", server=here.server))

        assert ids(call("keys", token)) == [mine.pk]
        assert call(f"keys/{other.pk}", token).status_code == 404

    def test_permissions_add_up(self, admin: User) -> None:
        first, second, third = UserFactory(), UserFactory(), UserFactory()
        token = issue(admin, Permission("users", user=first), Permission("users", user=second))

        assert ids(call("users", token)) == [first.pk, second.pk]
        assert call(f"users/{third.pk}", token).status_code == 404

    def test_bound_traffic_needs_a_named_object(self, admin: User) -> None:
        mine, other = ServerFactory(), ServerFactory()
        token = issue(admin, Permission("traffic", server=mine))

        assert call("traffic", token).status_code == 403
        assert call(f"traffic?server={other.pk}", token).status_code == 404
        assert call(f"traffic?server={mine.pk}", token).status_code == 200

    def test_a_deleted_object_takes_its_permission_with_it(self, admin: User) -> None:
        server = ServerFactory()
        token = issue(admin, Permission("servers", server=server))

        server.delete()

        assert not TokenPermission.objects.exists()
        assert call("servers", token).status_code == 403


class TestOwnerZone:
    def test_a_permission_outside_the_zone_is_not_given(self) -> None:
        moderator = UserFactory(role=Role.MODERATOR)

        with pytest.raises(PermissionZoneError):
            issue(moderator, Permission("servers", server=ServerFactory()))

    def test_a_whole_zone_permission_stays_within_the_zone(self) -> None:
        moderator = UserFactory(role=Role.MODERATOR)
        server, _user, _key = moderated(moderator)
        ServerFactory()
        token = issue(moderator, Permission("servers"))

        assert ids(call("servers", token)) == [server.pk]

    def test_a_token_loses_what_leaves_the_owners_zone(self) -> None:
        moderator = UserFactory(role=Role.MODERATOR)
        server, user, key = moderated(moderator)
        token = issue(
            moderator,
            Permission("servers", server=server),
            Permission("keys", "manage", user=user),
        )
        assert call(f"servers/{server.pk}", token).status_code == 200
        assert call(f"keys/{key.pk}", token).status_code == 200

        moderator.moderated_servers.remove(server)
        user.managers.remove(moderator)

        assert call(f"servers/{server.pk}", token).status_code == 404
        assert call(f"keys/{key.pk}", token).status_code == 404
        assert post(f"keys/{key.pk}/revoke", token).status_code == 404
        assert AccessKey.objects.get(pk=key.pk).status == KeyStatus.ACTIVE


class TestKeyChanges:
    def test_a_key_is_issued_by_an_operation(self, admin: User) -> None:
        instance = awg_instance()
        user = UserFactory()
        token = issue(admin, Permission("keys", "manage"), Permission("operations"))

        issued = {"source": instance.source, "user_id": user.pk, "name": "Pixel"}
        response = post("keys", token, issued)

        assert response.status_code == 202
        data = body(response)
        key = AccessKey.objects.get(pk=data["key_id"])
        assert (key.user, key.name, key.status) == (user, "Pixel", KeyStatus.ISSUING)
        (operation_id,) = data["operation_ids"]
        operation = Operation.objects.get(pk=operation_id)
        assert operation.kind == "keys.issue"
        status = body(call(f"operations/{operation_id}", token))
        assert (status["id"], status["kind"], status["status"]) == (
            operation_id,
            "keys.issue",
            "pending",
        )
        assert "params" not in status

    def test_a_user_permission_issues_only_to_that_user(self, admin: User) -> None:
        instance = awg_instance()
        mine, other = UserFactory(), UserFactory()
        token = issue(admin, Permission("keys", "manage", user=mine))

        refused = post("keys", token, {"source": instance.source, "user_id": other.pk, "name": "x"})

        assert refused.status_code == 403
        assert not AccessKey.objects.exists()

    def test_a_key_is_revoked_by_an_operation(self, admin: User) -> None:
        key = make_key(awg_instance(), UserFactory())
        token = issue(admin, Permission("keys", "manage", key=key))

        response = post(f"keys/{key.pk}/revoke", token)

        assert response.status_code == 202
        assert AccessKey.objects.get(pk=key.pk).status == KeyStatus.REVOKING
        assert Operation.objects.get(pk=body(response)["operation_ids"][0]).kind == "keys.revoke"
        again = post(f"keys/{key.pk}/revoke", token)
        assert again.status_code == 409
        assert Operation.objects.filter(kind="keys.revoke").count() == 1

    def test_reading_is_not_managing(self, admin: User) -> None:
        key = make_key(awg_instance(), UserFactory())
        token = issue(admin, Permission("keys"))

        assert post(f"keys/{key.pk}/revoke", token).status_code == 403
        assert AccessKey.objects.get(pk=key.pk).status == KeyStatus.ACTIVE

    def test_managing_needs_the_owner_to_manage_keys_in_the_panel(self) -> None:
        moderator = UserFactory(role=Role.MODERATOR)
        _server, _user, key = moderated(moderator)
        token = issue(moderator, Permission("keys", "manage"))
        moderator.access = {"keys": "view"}
        moderator.save(update_fields=["access"])

        assert post(f"keys/{key.pk}/revoke", token).status_code == 403
        assert call(f"keys/{key.pk}", token).status_code == 200

    def test_the_status_of_another_tokens_operation_is_hidden(self, admin: User) -> None:
        key = make_key(awg_instance(), UserFactory())
        first = issue(admin, Permission("keys", "manage"), Permission("operations"))
        second = issue(admin, Permission("operations"))
        operation_id = body(post(f"keys/{key.pk}/revoke", first))["operation_ids"][0]

        reader = issue(admin, Permission("keys"))
        assert call(f"operations/{operation_id}", second).status_code == 404
        assert call(f"operations/{operation_id}", reader).status_code == 403


class TestUserChanges:
    def test_access_is_extended(self, admin: User) -> None:
        user = UserFactory()
        end = timezone.now() + timedelta(days=3)
        UserLimitsFactory(user=user, access_until=end)
        token = issue(admin, Permission("users", "manage", user=user))

        response = post(f"users/{user.pk}/extend", token, {"days": 30})

        assert response.status_code == 202
        data = body(response)
        assert data["user_id"] == user.pk
        assert data["access_until"].startswith((end + timedelta(days=30)).date().isoformat())

    def test_access_of_a_user_outside_the_permission_stays(self, admin: User) -> None:
        mine, other = UserFactory(), UserFactory()
        UserLimitsFactory(user=other, access_until=timezone.now() + timedelta(days=3))
        token = issue(admin, Permission("users", "manage", user=mine))

        assert post(f"users/{other.pk}/extend", token, {"days": 30}).status_code == 404

    def test_extending_access_without_a_plan_is_a_conflict(self, admin: User) -> None:
        user = UserFactory()
        token = issue(admin, Permission("users", "manage"))

        response = post(f"users/{user.pk}/extend", token, {"days": 30})

        assert response.status_code == 409

    def test_the_plan_is_changed(self, admin: User) -> None:
        user = UserFactory()
        UserLimitsFactory(user=user)
        plan = PlanFactory()
        token = issue(admin, Permission("users", "manage"))

        response = post(f"users/{user.pk}/plan", token, {"plan_id": plan.pk})

        assert response.status_code == 202
        assert UserLimits.objects.get(user=user).plan == plan

    def test_a_plan_the_owner_may_not_give_is_refused(self) -> None:
        moderator = UserFactory(role=Role.MODERATOR)
        _server, user, _key = moderated(moderator)
        owner = UserFactory(role=Role.OWNER)
        plan = PlanFactory()
        plan.staff.add(owner)
        token = issue(moderator, Permission("users", "manage"))

        response = post(f"users/{user.pk}/plan", token, {"plan_id": plan.pk})

        assert response.status_code == 400
        assert body(response)["error"]["code"] == "bad_parameter"

    @pytest.mark.parametrize("payload", [b"[1]", b"not json", b'{"days": "30"}', b'{"days": 0}'])
    def test_a_bad_body_is_refused(self, admin: User, payload: bytes) -> None:
        user = UserFactory()
        token = issue(admin, Permission("users", "manage"))

        response = Client().post(
            f"/api/v1/users/{user.pk}/extend",
            payload,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token.value}",
        )

        assert response.status_code == 400


class TestMethods:
    def test_a_read_endpoint_refuses_post(self, admin: User) -> None:
        response = post("servers", issue(admin, Permission("servers")))

        assert response.status_code == 405
        assert response["Allow"] == "GET"

    def test_a_change_endpoint_refuses_get(self, admin: User) -> None:
        key = make_key(awg_instance(), UserFactory())

        response = call(f"keys/{key.pk}/revoke", issue(admin, Permission("keys", "manage")))

        assert response.status_code == 405
        assert response["Allow"] == "POST"

    def test_every_change_is_journaled(self, admin: User) -> None:
        key = make_key(awg_instance(), UserFactory())
        token = issue(admin, Permission("keys", "manage"))

        post(f"keys/{key.pk}/revoke", token)

        assert AuditEvent.objects.filter(action="api.call", data__status=202).exists()
        assert AuditEvent.objects.filter(action="keys.revoke", actor=admin).exists()


def test_openapi_describes_the_changes() -> None:
    paths = body(Client().get("/api/v1/openapi.json"))["paths"]

    assert set(paths["/keys"]) == {"get", "post"}
    for path in ("/users/{id}/extend", "/users/{id}/plan", "/keys/{id}/revoke"):
        assert "202" in paths[path]["post"]["responses"]
    assert "403" in paths["/operations/{id}"]["get"]["responses"]


def test_tokens_of_1_0_read_everything_after_the_migration(admin: User) -> None:
    token = ApiToken.objects.create(name="old", owner=admin, digest=digest("lmz_old"), prefix="x")
    migration = importlib.import_module("apps.api.migrations.0002_token_permissions")

    migration.read_everything(django_apps, None)

    rows = token.permissions.values_list("resource", "action", "server", "user", "key")
    expected = [
        (resource, "read", None, None, None)
        for resource in ("servers", "users", "keys", "traffic", "operations")
    ]
    assert sorted(map(tuple, rows)) == sorted(expected)
