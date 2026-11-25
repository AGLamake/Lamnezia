"""The OpenAPI 3.1 description of /api/v1/: paths, parameters and the shapes of the answers."""

from typing import Any

from apps.api.models import Action, Resource
from apps.api.services.query import DEFAULT_LIMIT, MAX_LIMIT
from apps.dashboard.services.stats import Period
from apps.keys.models import KeyStatus
from apps.operations.models import OperationStatus

VERSION = "1.1"

_STAMP = {"type": ["string", "null"], "format": "date-time"}
_ID = {"type": "integer"}
_NULL_ID = {"type": ["integer", "null"]}
_BYTES = {"type": "integer", "minimum": 0}
_NULL_BYTES = {"type": ["integer", "null"], "minimum": 0}


def _object(**properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties)}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _id_query(name: str, description: str) -> dict[str, Any]:
    return {"name": name, "in": "query", "description": description, "schema": _ID}


def _page(item: str) -> dict[str, Any]:
    return _object(count=_ID, limit=_ID, offset=_ID, results={"type": "array", "items": _ref(item)})


_SCHEMAS: dict[str, Any] = {
    "Error": _object(error=_object(code={"type": "string"}, message={"type": "string"})),
    "Token": _object(
        name={"type": "string"},
        prefix={"type": "string"},
        scope={"type": "string", "enum": list(Action.values)},
        permissions={"type": "array", "items": _ref("Permission")},
        expires_at=_STAMP,
        owner=_object(id=_ID, username={"type": "string"}, role={"type": "string"}),
        zone={"type": "string", "enum": ["whole", "moderator"]},
    ),
    "Permission": _object(
        resource={"type": "string", "enum": list(Resource.values)},
        action={"type": "string", "enum": list(Action.values)},
        server_id=_NULL_ID,
        user_id=_NULL_ID,
        key_id=_NULL_ID,
    ),
    "Started": {
        "type": "object",
        "description": "The change is accepted; follow its operations at /operations/{id}.",
        "properties": {
            "operation_ids": {"type": "array", "items": _ID},
            "key_id": _ID,
            "user_id": _ID,
            "plan_id": _ID,
            "access_until": _STAMP,
        },
        "required": ["operation_ids"],
    },
    "Operation": _object(
        id=_ID,
        kind={"type": "string"},
        status={"type": "string", "enum": list(OperationStatus.values)},
        created_at={"type": "string", "format": "date-time"},
        started_at=_STAMP,
        finished_at=_STAMP,
        error={
            "oneOf": [
                _object(code={"type": "string"}, message={"type": "string"}),
                {"type": "null"},
            ]
        },
    ),
    "Server": _object(
        id=_ID,
        name={"type": "string"},
        host={"type": "string"},
        location={"type": "string"},
        status={"type": "string"},
        online={"type": "boolean"},
        maintenance={"type": "boolean"},
        paid_until={"type": ["string", "null"], "format": "date"},
        checked_at=_STAMP,
        last_seen_at=_STAMP,
        load={
            "oneOf": [
                _object(
                    at={"type": "string", "format": "date-time"},
                    answered={"type": "boolean"},
                    cpu_percent={"type": ["number", "null"]},
                    memory_percent={"type": ["number", "null"]},
                    disk_percent={"type": ["number", "null"]},
                    uptime_seconds={"type": ["integer", "null"]},
                ),
                {"type": "null"},
            ]
        },
        protocols={
            "type": "array",
            "items": _object(
                source={"type": "string"},
                container={"type": "string"},
                title={"type": "string"},
                port={"type": ["integer", "null"]},
                removed={"type": "boolean"},
            ),
        },
    ),
    "User": _object(
        id=_ID,
        username={"type": "string"},
        email={"type": "string"},
        role={"type": "string"},
        status={"type": "string"},
        is_active={"type": "boolean"},
        joined_at={"type": "string", "format": "date-time"},
        moderator_id=_NULL_ID,
        moderator_ids={"type": "array", "items": _ID},
        plan={
            "oneOf": [
                _object(id=_ID, name={"type": "string"}, is_trial={"type": "boolean"}),
                {"type": "null"},
            ]
        },
        limits=_object(
            traffic_bytes=_NULL_BYTES,
            period_days={"type": ["integer", "null"]},
            total_traffic_bytes=_NULL_BYTES,
            max_keys={"type": ["integer", "null"]},
            speed_mbit={"type": ["integer", "null"]},
            access_until=_STAMP,
        ),
        usage=_object(
            period_bytes=_BYTES,
            total_bytes=_BYTES,
            resets_at=_STAMP,
            expired={"type": "boolean"},
        ),
    ),
    "Key": _object(
        id=_ID,
        name={"type": "string"},
        user_id=_NULL_ID,
        server_id=_NULL_ID,
        source={"type": "string"},
        protocol={"type": "string"},
        status={"type": "string", "enum": list(KeyStatus.values)},
        imported={"type": "boolean"},
        created_at={"type": "string", "format": "date-time"},
        first_viewed_at=_STAMP,
        revoked_at=_STAMP,
    ),
    "Traffic": _object(
        period={"type": "string", "enum": list(Period.values)},
        bucket={"type": "string", "enum": ["hour", "day"]},
        since={"type": "string", "format": "date-time"},
        series={
            "type": "array",
            "items": _object(
                at={"type": "string", "format": "date-time"}, rx_bytes=_BYTES, tx_bytes=_BYTES
            ),
        },
        keys={
            "type": "array",
            "items": _object(
                key_id=_ID, rx_bytes=_BYTES, tx_bytes=_BYTES, known={"type": "boolean"}
            ),
        },
    ),
}

_ERRORS = {
    "400": "A query parameter or the body has a wrong value.",
    "401": "No token, an unknown token, or one that is disabled or expired.",
    "403": "No permission of the token gives this resource and action.",
    "404": "No such object in the token's permissions and its owner's zone.",
    "405": "The method is not allowed here; see Allow.",
    "429": "Too many calls this minute; see Retry-After.",
}
_CONFLICT = {"409": "The object's state does not allow the change now."}

_PAGING = [
    {
        "name": "limit",
        "in": "query",
        "schema": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
    },
    {"name": "offset", "in": "query", "schema": {"type": "integer", "minimum": 0, "default": 0}},
]

_PK = {"name": "id", "in": "path", "required": True, "schema": _ID}


def _json(description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def _responses(success: dict[str, Any], errors: dict[str, str]) -> dict[str, Any]:
    return {**success, **{code: _json(text, _ref("Error")) for code, text in errors.items()}}


def _get(summary: str, schema: dict[str, Any], parameters: list[Any] | None = None) -> Any:
    responses = _responses({"200": _json("OK", schema)}, _ERRORS)
    return {"get": {"summary": summary, "parameters": parameters or [], "responses": responses}}


def _post(summary: str, body: dict[str, Any] | None, parameters: list[Any]) -> dict[str, Any]:
    """A change: it starts operations and answers 202 at once."""
    accepted = {"202": _json("Accepted", _ref("Started"))}
    operation: dict[str, Any] = {
        "summary": summary,
        "parameters": parameters,
        "responses": _responses(accepted, {**_ERRORS, **_CONFLICT}),
    }
    if body is not None:
        content = {"application/json": {"schema": body}}
        operation["requestBody"] = {"required": True, "content": content}
    return {"post": operation}


def document(server_url: str) -> dict[str, Any]:
    """The description, with `server_url` as the base of the paths."""
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Lamnezia API",
            "version": VERSION,
            "description": (
                "Access to the panel for integrations. A token reaches what its owner's zone "
                "holds at the moment of the call, narrowed by its permissions: a resource, "
                "read or manage, and optionally one server, user or key. Changes answer 202 "
                "with the operations that carry them out. No secrets are returned."
            ),
        },
        "servers": [{"url": server_url}],
        "security": [{"token": []}],
        "paths": {
            "/token": _get("The token of the call", _ref("Token")),
            "/servers": _get("Servers and their state", _page("Server"), _PAGING),
            "/servers/{id}": _get("A server", _ref("Server"), [_PK]),
            "/users": _get("Users, their limits and usage", _page("User"), _PAGING),
            "/users/{id}": _get("A user", _ref("User"), [_PK]),
            "/users/{id}/extend": _post(
                "Extend the user's access by a number of days",
                _object(days={"type": "integer", "minimum": 1, "maximum": 3650}),
                [_PK],
            ),
            "/users/{id}/plan": _post(
                "Move the user to a plan the token's owner may give", _object(plan_id=_ID), [_PK]
            ),
            "/keys": {
                **_get(
                    "Access keys, without key material",
                    _page("Key"),
                    [
                        *_PAGING,
                        _id_query("user", "Keys of this user."),
                        _id_query("server", "Keys on this server."),
                        {
                            "name": "status",
                            "in": "query",
                            "schema": {"type": "string", "enum": list(KeyStatus.values)},
                        },
                    ],
                ),
                **_post(
                    "Issue a key on an instance (its source) to a user",
                    _object(source={"type": "string"}, user_id=_ID, name={"type": "string"}),
                    [],
                ),
            },
            "/keys/{id}": _get("An access key", _ref("Key"), [_PK]),
            "/keys/{id}/revoke": _post("Revoke the key", None, [_PK]),
            "/operations/{id}": _get(
                "The status of an operation the token started", _ref("Operation"), [_PK]
            ),
            "/traffic": _get(
                "Traffic by hour or day and per key",
                _ref("Traffic"),
                [
                    {
                        "name": "period",
                        "in": "query",
                        "schema": {
                            "type": "string",
                            "enum": list(Period.values),
                            "default": Period.DAY.value,
                        },
                    },
                    _id_query("server", "Only this server."),
                    _id_query("user", "Only this user."),
                    _id_query("key", "Only this key."),
                ],
            ),
        },
        "components": {
            "schemas": _SCHEMAS,
            "securitySchemes": {"token": {"type": "http", "scheme": "bearer"}},
        },
    }
