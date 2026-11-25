"""API endpoints under /api/v1/: a token in the header, JSON out, every call in the audit."""

from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views import View

from apps.accounts.services.zone import Zone
from apps.api.errors import (
    ApiError,
    MethodNotAllowedError,
    NotFoundError,
    TokenRejectedError,
)
from apps.api.models import Action, ApiToken, Resource
from apps.api.openapi import document
from apps.api.services.gate import ApiGate, record_call, record_refusal
from apps.api.services.keys import KeyRecords
from apps.api.services.manage import TokenKeyActions, TokenUserActions, access_until
from apps.api.services.operations import OperationRecords
from apps.api.services.permissions import Reach, TokenGrants
from apps.api.services.query import Body, Page, choice_param, id_param
from apps.api.services.servers import ServerRecords
from apps.api.services.traffic import TrafficReport
from apps.api.services.users import UserRecords
from apps.audit.services.journal import client_ip
from apps.dashboard.services.stats import Period
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.zone import KeyZone

# A change answers 202: the operations it started do the rest.
_ACCEPTED = 202
_MAX_DAYS = 3650


def error_response(error: ApiError) -> JsonResponse:
    response = JsonResponse(
        {"error": {"code": error.code, "message": error.message}}, status=error.status
    )
    for name, value in error.headers().items():
        response[name] = value
    return response


class ApiView(View):
    """Lets the call in through `ApiGate`, answers with `read` (GET) or `write` (POST) and
    journals the call. A view lists the methods it answers in `methods`."""

    methods: tuple[str, ...] = ("GET",)
    token: ApiToken
    grants: TokenGrants

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ip = client_ip(request)
        path = request.get_full_path()
        try:
            if request.method not in self.methods:
                raise MethodNotAllowedError(self.methods)
            self.token = ApiGate().admit(request.headers.get("Authorization", ""), ip)
        except TokenRejectedError as refused:
            record_refusal(refused.token, path=path, status=refused.status, ip=ip)
            return error_response(refused)
        except ApiError as error:
            return error_response(error)
        self.grants = TokenGrants(self.token)
        try:
            if request.method == "POST":
                response = JsonResponse(self.write(request, **kwargs), status=_ACCEPTED)
            else:
                response = JsonResponse(self.read(request, **kwargs))
        except ApiError as error:
            response = error_response(error)
        record_call(self.token, path=path, status=response.status_code, ip=ip)
        return response

    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        """The answer of a GET; raise an ApiError to refuse it."""
        raise MethodNotAllowedError(self.methods)

    def write(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        """Start the change of a POST and answer with its operations."""
        raise MethodNotAllowedError(self.methods)

    def reach(self, resource: str, action: str = Action.READ) -> Reach:
        return self.grants.reach(resource, action)

    @property
    def ip(self) -> str | None:
        return client_ip(self.request)

    @property
    def zone(self) -> Zone:
        return Zone(self.token.owner)

    @property
    def key_zone(self) -> KeyZone:
        return KeyZone(self.token.owner)


class TokenView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        token, owner = self.token, self.token.owner
        permissions = self.grants.described()
        manages = any(row["action"] == Action.MANAGE for row in permissions)
        return {
            "name": token.name,
            "prefix": token.prefix,
            # Kept for 1.0 clients: "manage" once any permission manages something.
            "scope": Action.MANAGE.value if manages else Action.READ.value,
            "permissions": permissions,
            "expires_at": token.expires_at,
            "owner": {"id": owner.pk, "username": owner.username, "role": owner.role},
            "zone": "whole" if self.zone.is_whole else "moderator",
        }


class ServerListView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        page = Page.of(request.GET)
        records = ServerRecords(self.zone, self.reach(Resource.SERVERS))
        count, servers = page.items(records.rows())
        return page.answer(count, [ServerRecords.record(server) for server in servers])


class ServerDetailView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        records = ServerRecords(self.zone, self.reach(Resource.SERVERS))
        return ServerRecords.record(records.get(kwargs["pk"]))


class UserListView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        page = Page.of(request.GET)
        records = UserRecords(self.zone, self.reach(Resource.USERS))
        count, users = page.items(records.rows())
        return page.answer(count, records.records(users))


class UserDetailView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        records = UserRecords(self.zone, self.reach(Resource.USERS))
        return records.records([records.get(kwargs["pk"])])[0]


class UserExtendView(ApiView):
    methods = ("POST",)

    def write(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        reach = self.reach(Resource.USERS, Action.MANAGE)
        user = UserRecords(self.zone, reach).get(kwargs["pk"])
        days = Body(request.body).integer("days", high=_MAX_DAYS)
        started = TokenUserActions(self.token, reach, self.ip).extend(user, days)
        return started.answer(user_id=user.pk, access_until=access_until(user))


class UserPlanView(ApiView):
    methods = ("POST",)

    def write(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        reach = self.reach(Resource.USERS, Action.MANAGE)
        user = UserRecords(self.zone, reach).get(kwargs["pk"])
        plan_id = Body(request.body).integer("plan_id")
        started = TokenUserActions(self.token, reach, self.ip).change_plan(user, plan_id)
        return started.answer(user_id=user.pk, plan_id=plan_id, access_until=access_until(user))


class KeyListView(ApiView):
    methods = ("GET", "POST")

    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        page = Page.of(request.GET)
        records = KeyRecords(self.key_zone, self.reach(Resource.KEYS))
        rows = records.rows(
            user=id_param(request.GET, "user"),
            server=id_param(request.GET, "server"),
            status=choice_param(request.GET, "status", KeyStatus.values),
        )
        count, keys = page.items(rows)
        return page.answer(count, records.records(keys))

    def write(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        """Issue a key: `source` (an instance), `user_id` and `name` (the device)."""
        reach = self.reach(Resource.KEYS, Action.MANAGE)
        body = Body(request.body)
        name_length = AccessKey._meta.get_field("name").max_length or 100
        key, started = TokenKeyActions(self.token, reach, self.ip).issue(
            source=body.text("source", 100),
            name=body.text("name", name_length),
            user_id=body.integer("user_id"),
        )
        return started.answer(key_id=key.pk)


class KeyDetailView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        records = KeyRecords(self.key_zone, self.reach(Resource.KEYS))
        return records.records([records.get(kwargs["pk"])])[0]


class KeyRevokeView(ApiView):
    methods = ("POST",)

    def write(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        reach = self.reach(Resource.KEYS, Action.MANAGE)
        key = KeyRecords(self.key_zone, reach).get(kwargs["pk"])
        started = TokenKeyActions(self.token, reach, self.ip).revoke(key)
        return started.answer(key_id=key.pk)


class TrafficView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        query = request.GET
        period = choice_param(query, "period", Period.values) or Period.DAY
        server_id, user_id, key_id = (id_param(query, name) for name in ("server", "user", "key"))
        report = TrafficReport(self.zone, self.key_zone, self.reach(Resource.TRAFFIC))
        return report.answer(Period(period), server=server_id, user=user_id, key=key_id)


class OperationDetailView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        self.reach(Resource.OPERATIONS)
        records = OperationRecords(self.token)
        return records.record(records.get(kwargs["pk"]))


class OpenApiView(View):
    """The description of the API; it holds no panel data, so it needs no token."""

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.method != "GET":
            return error_response(MethodNotAllowedError())
        return JsonResponse(document(request.build_absolute_uri("/api/v1")))


def not_found(request: HttpRequest, path: str) -> JsonResponse:
    """Any other address under /api/: a JSON 404 rather than the panel's page."""
    return error_response(NotFoundError())
