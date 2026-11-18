"""API endpoints under /api/v1/: a token in the header, JSON out, every call in the audit."""

from abc import abstractmethod
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
from apps.api.models import ApiToken
from apps.api.openapi import document
from apps.api.services.gate import ApiGate, record_call, record_refusal
from apps.api.services.keys import KeyRecords
from apps.api.services.query import Page, choice_param, id_param
from apps.api.services.servers import ServerRecords
from apps.api.services.traffic import TrafficReport
from apps.api.services.users import UserRecords
from apps.audit.services.journal import client_ip
from apps.dashboard.services.stats import Period
from apps.keys.models import KeyStatus
from apps.keys.services.zone import KeyZone


def error_response(error: ApiError) -> JsonResponse:
    response = JsonResponse(
        {"error": {"code": error.code, "message": error.message}}, status=error.status
    )
    for name, value in error.headers().items():
        response[name] = value
    return response


class ApiView(View):
    """Lets the call in through `ApiGate`, answers with `read` and journals the call."""

    token: ApiToken

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ip = client_ip(request)
        path = request.get_full_path()
        try:
            if request.method != "GET":
                raise MethodNotAllowedError
            self.token = ApiGate().admit(request.headers.get("Authorization", ""), ip)
        except TokenRejectedError as refused:
            record_refusal(refused.token, path=path, status=refused.status, ip=ip)
            return error_response(refused)
        except ApiError as error:
            return error_response(error)
        try:
            response = JsonResponse(self.read(request, **kwargs))
        except ApiError as error:
            response = error_response(error)
        record_call(self.token, path=path, status=response.status_code, ip=ip)
        return response

    @abstractmethod
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        """The answer of the call; raise an ApiError to refuse it."""

    @property
    def zone(self) -> Zone:
        return Zone(self.token.owner)

    @property
    def key_zone(self) -> KeyZone:
        return KeyZone(self.token.owner)


class TokenView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        token, owner = self.token, self.token.owner
        return {
            "name": token.name,
            "prefix": token.prefix,
            "scope": token.scope,
            "expires_at": token.expires_at,
            "owner": {"id": owner.pk, "username": owner.username, "role": owner.role},
            "zone": "whole" if self.zone.is_whole else "moderator",
        }


class ServerListView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        page = Page.of(request.GET)
        count, servers = page.items(ServerRecords(self.zone).rows())
        return page.answer(count, [ServerRecords.record(server) for server in servers])


class ServerDetailView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        return ServerRecords.record(ServerRecords(self.zone).get(kwargs["pk"]))


class UserListView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        page = Page.of(request.GET)
        records = UserRecords(self.zone)
        count, users = page.items(records.rows())
        return page.answer(count, records.records(users))


class UserDetailView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        records = UserRecords(self.zone)
        return records.records([records.get(kwargs["pk"])])[0]


class KeyListView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        page = Page.of(request.GET)
        records = KeyRecords(self.key_zone)
        rows = records.rows(
            user=id_param(request.GET, "user"),
            server=id_param(request.GET, "server"),
            status=choice_param(request.GET, "status", KeyStatus.values),
        )
        count, keys = page.items(rows)
        return page.answer(count, records.records(keys))


class KeyDetailView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        records = KeyRecords(self.key_zone)
        return records.records([records.get(kwargs["pk"])])[0]


class TrafficView(ApiView):
    def read(self, request: HttpRequest, **kwargs: Any) -> dict[str, Any]:
        query = request.GET
        period = choice_param(query, "period", Period.values) or Period.DAY
        server_id, user_id, key_id = (id_param(query, name) for name in ("server", "user", "key"))
        return TrafficReport(self.key_zone).answer(
            Period(period),
            server=ServerRecords(self.zone).get(server_id) if server_id else None,
            user=UserRecords(self.zone).get(user_id) if user_id else None,
            key=KeyRecords(self.key_zone).get(key_id) if key_id else None,
        )


class OpenApiView(View):
    """The description of the API; it holds no panel data, so it needs no token."""

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.method != "GET":
            return error_response(MethodNotAllowedError())
        return JsonResponse(document(request.build_absolute_uri("/api/v1")))


def not_found(request: HttpRequest, path: str) -> JsonResponse:
    """Any other address under /api/: a JSON 404 rather than the panel's page."""
    return error_response(NotFoundError())
