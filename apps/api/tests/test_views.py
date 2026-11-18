import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.api.models import ApiToken
from apps.api.services.tokens import ApiTokens, IssuedToken
from apps.audit.models import AuditEvent
from apps.backups.tests.helpers import awg_instance
from apps.keys.models import AccessKey, KeyStatus
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.protocols.models import ProtocolInstance
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.traffic.models import TrafficHourly

pytestmark = pytest.mark.django_db

PASSWORD = "server-password-value"
KEY_PRIVATE = "client-private-key-value"
KEY_PROFILE = "client-profile-value"
PEER = "client-uuid-or-public-key"


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


@pytest.fixture
def moderator() -> User:
    return UserFactory(role=Role.MODERATOR)


def issue(owner: User, **kwargs: Any) -> IssuedToken:
    return ApiTokens().issue(name="integration", owner=owner, by=owner, **kwargs)


def call(path: str, issued: IssuedToken | None = None, method: str = "get") -> Any:
    headers = {"HTTP_AUTHORIZATION": f"Bearer {issued.value}"} if issued else {}
    return getattr(Client(), method)(f"/api/v1/{path}", **headers)


def body(response: Any) -> Any:
    return json.loads(response.content)


def make_key(instance: ProtocolInstance, user: User | None, name: str = "phone") -> AccessKey:
    return AccessKey.objects.create(
        source=instance.source,
        user=user,
        name=name,
        public_key=PEER if name == "phone" else f"{PEER}-{name}",
        private_key=KEY_PRIVATE,
        profile=KEY_PROFILE,
        status=KeyStatus.ACTIVE,
    )


def moderated(moderator: User) -> tuple[Server, User, AccessKey]:
    """A server the moderator looks after, a user of theirs and the user's key on it."""
    instance = awg_instance()
    moderator.moderated_servers.add(instance.server)
    user = UserFactory(manager=moderator)
    return instance.server, user, make_key(instance, user)


# --- the way in ---------------------------------------------------------------------------


def test_no_token_is_401_with_a_json_error() -> None:
    response = call("token")

    assert response.status_code == 401
    assert body(response)["error"]["code"] == "token_missing"
    assert response["WWW-Authenticate"].startswith("Bearer")


def test_an_unknown_token_is_401_and_not_journaled() -> None:
    response = Client().get("/api/v1/token", HTTP_AUTHORIZATION="Bearer lmz_unknown")

    assert response.status_code == 401
    assert body(response)["error"]["code"] == "token_invalid"
    assert not AuditEvent.objects.filter(action__startswith="api.").exists()


def test_a_disabled_token_is_401_and_journaled_as_denied(admin: User) -> None:
    issued = issue(admin)
    ApiTokens().deactivate(issued.token)

    response = call("token", issued)

    assert response.status_code == 401
    event = AuditEvent.objects.get(action="api.denied")
    assert event.data["token"] == issued.token.prefix


def test_an_expired_token_is_401(admin: User) -> None:
    issued = issue(admin, expires_at=timezone.now() - timedelta(minutes=1))

    assert call("token", issued).status_code == 401


def test_only_get_is_allowed(admin: User) -> None:
    response = call("servers", issue(admin), method="post")

    assert response.status_code == 405
    assert response["Allow"] == "GET"


def test_too_many_calls_are_429(admin: User, settings: Any) -> None:
    settings.API_RATE_LIMIT = 2
    issued = issue(admin)

    assert [call("token", issued).status_code for _ in range(3)] == [200, 200, 429]
    assert call("token", issued)["Retry-After"] == "60"


def test_a_call_is_journaled_and_marks_the_token_used(admin: User) -> None:
    issued = issue(admin)

    call("servers?limit=5", issued)

    event = AuditEvent.objects.get(action="api.call")
    assert event.data == {
        "token": issued.token.prefix,
        "path": "/api/v1/servers?limit=5",
        "status": 200,
    }
    assert event.actor == admin
    assert ApiToken.objects.get(pk=issued.token.pk).last_used_at is not None


def test_an_unknown_address_is_a_json_404(admin: User) -> None:
    response = call("nothing/here", issue(admin))

    assert response.status_code == 404
    assert body(response)["error"]["code"] == "not_found"


def test_the_token_describes_itself(moderator: User) -> None:
    issued = issue(moderator)

    data = body(call("token", issued))

    assert data["prefix"] == issued.token.prefix
    assert data["scope"] == "read"
    assert data["zone"] == "moderator"
    assert data["owner"]["username"] == moderator.username


# --- no secrets ---------------------------------------------------------------------------


def test_answers_hold_no_secrets(admin: User) -> None:
    instance = awg_instance(ServerFactory(password=PASSWORD, passphrase="key-passphrase"))
    user = UserFactory()
    key = make_key(instance, user)
    issued = issue(admin)

    texts = [
        call(path, issued).content.decode()
        for path in (
            "servers",
            f"servers/{instance.server.pk}",
            "users",
            f"users/{user.pk}",
            "keys",
            f"keys/{key.pk}",
            "traffic",
        )
    ]

    secrets = [
        "secret-key-body",
        PASSWORD,
        "key-passphrase",
        instance.private_key,
        instance.preshared_key,
        instance.public_key,
        KEY_PRIVATE,
        KEY_PROFILE,
        PEER,
        issued.value,
    ]
    for text in texts:
        for secret in secrets:
            assert secret not in text


def test_server_records_skip_ssh_access(admin: User) -> None:
    ServerFactory()

    record = body(call("servers", issue(admin)))["results"][0]

    assert not {"username", "port", "auth_method", "password", "private_key"} & set(record)


# --- servers ------------------------------------------------------------------------------


def test_servers_list_with_protocols(admin: User) -> None:
    instance = awg_instance()

    data = body(call("servers", issue(admin)))

    assert data["count"] == 1
    server = data["results"][0]
    assert server["id"] == instance.server.pk
    assert server["protocols"][0]["container"] == "amnezia-awg2"
    assert server["load"] is None


def test_a_moderator_sees_only_their_servers(moderator: User) -> None:
    mine, _user, _key = moderated(moderator)
    other = ServerFactory()
    issued = issue(moderator)

    assert [row["id"] for row in body(call("servers", issued))["results"]] == [mine.pk]
    assert call(f"servers/{other.pk}", issued).status_code == 404
    assert call(f"servers/{mine.pk}", issued).status_code == 200


def test_pages_of_a_list(admin: User) -> None:
    servers = [ServerFactory() for _ in range(3)]
    issued = issue(admin)

    data = body(call("servers?limit=2&offset=1", issued))

    assert data["count"] == 3
    assert (data["limit"], data["offset"]) == (2, 1)
    assert [row["id"] for row in data["results"]] == [server.pk for server in servers[1:]]


@pytest.mark.parametrize("query", ["limit=0", "limit=1000", "offset=-1", "limit=x"])
def test_a_wrong_page_is_400(admin: User, query: str) -> None:
    response = call(f"servers?{query}", issue(admin))

    assert response.status_code == 400
    assert body(response)["error"]["code"] == "bad_parameter"


# --- users --------------------------------------------------------------------------------


def test_users_have_plan_limits_and_usage(admin: User) -> None:
    user = UserFactory()
    plan = PlanFactory(name="Basic", traffic_limit=10_000)
    UserLimitsFactory(user=user, plan=plan)

    record = body(call(f"users/{user.pk}", issue(admin)))

    assert record["plan"]["name"] == "Basic"
    assert record["limits"]["traffic_bytes"] == 10_000
    assert record["usage"]["period_bytes"] == 0
    assert "password" not in record


def test_a_moderator_sees_only_their_users(moderator: User) -> None:
    _server, mine, _key = moderated(moderator)
    stranger = UserFactory()
    issued = issue(moderator)

    assert [row["id"] for row in body(call("users", issued))["results"]] == [mine.pk]
    assert call(f"users/{stranger.pk}", issued).status_code == 404


# --- keys ---------------------------------------------------------------------------------


def test_keys_name_server_and_protocol(admin: User) -> None:
    instance = awg_instance()
    key = make_key(instance, UserFactory())

    record = body(call(f"keys/{key.pk}", issue(admin)))

    assert record["server_id"] == instance.server.pk
    assert record["protocol"] == "amnezia-awg2"
    assert record["status"] == KeyStatus.ACTIVE


def test_keys_are_filtered(admin: User) -> None:
    first, second = awg_instance(), awg_instance()
    user = UserFactory()
    on_first = make_key(first, user)
    make_key(second, UserFactory(), name="laptop")
    issued = issue(admin)

    def ids(query: str) -> list[int]:
        return [row["id"] for row in body(call(f"keys?{query}", issued))["results"]]

    assert ids(f"server={first.server.pk}") == [on_first.pk]
    assert ids(f"user={user.pk}") == [on_first.pk]
    assert ids("status=revoked") == []
    assert call("keys?status=gone", issued).status_code == 400


def test_a_moderator_sees_only_keys_of_their_zone(moderator: User) -> None:
    _server, _user, mine = moderated(moderator)
    foreign = make_key(awg_instance(), UserFactory(), name="foreign")
    issued = issue(moderator)

    assert [row["id"] for row in body(call("keys", issued))["results"]] == [mine.pk]
    assert call(f"keys/{foreign.pk}", issued).status_code == 404


# --- traffic ------------------------------------------------------------------------------


def test_traffic_by_hour_and_by_key(admin: User) -> None:
    instance = awg_instance()
    key = make_key(instance, UserFactory())
    hour = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    TrafficHourly.objects.create(
        source=instance.source, public_key=key.public_key, hour=hour, rx_bytes=300, tx_bytes=700
    )

    data = body(call("traffic", issue(admin)))

    assert data["period"] == "day"
    assert data["bucket"] == "hour"
    assert sum(point["rx_bytes"] for point in data["series"]) == 300
    assert {"key_id": key.pk, "rx_bytes": 300, "tx_bytes": 700, "known": True} in data["keys"]


def test_traffic_of_an_object_outside_the_zone_is_404(moderator: User) -> None:
    moderated(moderator)
    issued = issue(moderator)

    assert call(f"traffic?server={ServerFactory().pk}", issued).status_code == 404
    assert call(f"traffic?user={UserFactory().pk}", issued).status_code == 404
    assert call("traffic?period=year", issued).status_code == 400


# --- OpenAPI ------------------------------------------------------------------------------


def test_openapi_needs_no_token_and_lists_the_paths() -> None:
    response = call("openapi.json")

    assert response.status_code == 200
    data = body(response)
    assert data["openapi"] == "3.1.0"
    assert {"/token", "/servers", "/users", "/keys", "/traffic"} <= set(data["paths"])
    assert data["servers"][0]["url"].endswith("/api/v1")
