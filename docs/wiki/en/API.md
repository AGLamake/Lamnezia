# API

The panel API lets your billing, bots and monitoring read the panel: servers and their state,
users with their limits and usage, keys and traffic. Version 1 only reads; nothing in the panel
or on the servers changes through it.

## Tokens

**System → API tokens** (`/settings/api/`, administrators only) lists the tokens and creates new
ones. A token has:

- a **name**: what uses it, e.g. `monitoring`;
- **acts as**: an administrator or a moderator. The token sees what this person sees: an
  administrator's token sees the whole panel, a moderator's token only the moderator's servers,
  users and keys;
- a **scope**: `read`, the only one so far;
- an optional **expiry**.

The token value (`lmz_…`) is shown once, right after it is created: copy it then. The panel keeps
only its hash and the first characters, to tell tokens apart in the list.

A token stops working when it is disabled, when its expiry is past, or when its owner is blocked
or is no longer an administrator or moderator. **Disable** cannot be undone: create a new token
instead.

## Request

Send the token in the `Authorization` header; the panel does not take it in the address, so it does
not end up in proxy logs:

```sh
curl -H "Authorization: Bearer lmz_..." https://panel.example.com/api/v1/servers
```

Only `GET`. Answers are JSON with times in ISO 8601 and traffic in bytes. Lists come in pages:
`limit` (50 by default, up to 200) and `offset`; the answer holds `count` (the whole list),
`limit`, `offset` and `results`.

Errors come as `{"error": {"code": "...", "message": "..."}}`:

| Status | `code` | When |
|---|---|---|
| 400 | `bad_parameter` | A query parameter has a wrong value. |
| 401 | `token_missing`, `token_invalid` | No token, an unknown one, or one that does not work. |
| 404 | `not_found` | No such object, or it is outside the token's zone. |
| 405 | `method_not_allowed` | A method other than `GET`. |
| 429 | `too_many_calls` | Over `LAMNEZIA_API_RATE_LIMIT` calls a minute; wait `Retry-After` seconds. |

## Endpoints

All under `/api/v1/`:

| Path | What |
|---|---|
| `token` | The token of the call: name, scope, expiry, owner, `zone` (`whole` or `moderator`). |
| `servers`, `servers/<id>` | State, online, maintenance, paid until, the latest load reading, protocols. |
| `users`, `users/<id>` | Role, status, plan, moderators (`moderator_ids`; `moderator_id` is the first of them), effective limits and usage of the period and in total. |
| `keys`, `keys/<id>` | Owner, server, protocol, status, dates. Filters: `user`, `server`, `status`. |
| `traffic` | Traffic by hour (`period=day`) or by day (`week`, `month`) and per key. Filters: `server`, `user`, `key`. |

The full description is OpenAPI 3.1 at `/api/v1/openapi.json`; it needs no token, as it holds no
panel data. Load it into Swagger UI, Postman or a client generator.

## What the API never returns

SSH access to servers, server keys and protocol settings, private keys and profiles of clients,
the public key of a key (for XRay it is the client UUID, the access itself), subscription links,
passwords and 2FA data.

## Rate and audit

One token makes at most `LAMNEZIA_API_RATE_LIMIT` calls a minute (120 by default, `0` turns the
limit off, [Configuration](Configuration.md)). Calls with a wrong token are limited the same way by
the client address, so guessing tokens gets nowhere.

Every call with a working token goes to the [Audit log](Audit.md) as **API call** with the token
prefix, the path and the status. A call with a disabled or expired token is **API call with a
disabled token**. Calls with unknown tokens are not written, so guessing does not flood the log.
Creating and disabling a token are written too. **API call** records are kept for 30 days
(`LAMNEZIA_API_CALL_RETENTION_DAYS`, [Configuration](Configuration.md)); older ones are deleted
once a day. Other records of the log, calls with a disabled token included, are not deleted.
