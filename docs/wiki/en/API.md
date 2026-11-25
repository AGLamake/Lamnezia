# API

The panel API lets your billing, bots and monitoring read the panel: servers and their state,
users with their limits and usage, keys and traffic, and, with a manage permission, issue and
revoke keys, extend access and change plans.

## Tokens

**System → API tokens** (`/settings/api/`) is where each staff member (administrator or moderator)
issues tokens for themselves and sees only their own. The administrator also sees and can disable
everyone's tokens. A token has:

- a **name**: what uses it, e.g. `monitoring`;
- **permissions**: what the token may do (below);
- an optional **expiry**.

A token acts as the one who issued it and never sees more than this person: an administrator's
token sees the whole panel, a moderator's token only the moderator's servers, users and keys. The
zone is taken at the moment of the call: when a server or a user leaves a moderator's zone, the
moderator's tokens stop seeing it.

The token value (`lmz_…`) is shown once, right after it is created: copy it then. The panel keeps
only its hash and the first characters, to tell tokens apart in the list.

A token stops working when it is disabled, when its expiry is past, or when its owner is blocked
or is no longer an administrator or moderator. **Disable** cannot be undone: create a new token
instead.

## Permissions

A permission is a resource, an action and, if needed, one object. The permission rows of the
builder add up; at least one is needed.

| Resource | Actions | Object |
|---|---|---|
| Servers (`servers`) | read | a server |
| Users (`users`) | read, manage | a user |
| Keys (`keys`) | read, manage | a server, a user or a key |
| Traffic (`traffic`) | read | a server, a user or a key |
| Operation status (`operations`) | read | — |

- **Manage** includes read.
- **Without an object** a permission covers your whole zone. With one it covers only that object:
  a keys permission with a server gives the keys on that server, with a user that user's keys.
- The builder offers only the servers, users and keys of your zone.
- Managing through the API works only while you manage that section in the panel yourself: if your
  role was left with view only in **Keys**, the token cannot change keys either.
- Tokens created before permissions came read every resource without an object: their access did
  not change.

## Request

Send the token in the `Authorization` header; the panel does not take it in the address, so it does
not end up in proxy logs:

```sh
curl -H "Authorization: Bearer lmz_..." https://panel.example.com/api/v1/servers
```

Reading is `GET`, changes are `POST` with a JSON body:

```sh
curl -X POST -H "Authorization: Bearer lmz_..." -H "Content-Type: application/json" \
  -d '{"days": 30}' https://panel.example.com/api/v1/users/42/extend
```

Answers are JSON with times in ISO 8601 and traffic in bytes. Lists come in pages:
`limit` (50 by default, up to 200) and `offset`; the answer holds `count` (the whole list),
`limit`, `offset` and `results`.

Errors come as `{"error": {"code": "...", "message": "..."}}`:

| Status | `code` | When |
|---|---|---|
| 400 | `bad_parameter`, `bad_body` | A query parameter or a body field has a wrong value; the body is not a JSON object. |
| 401 | `token_missing`, `token_invalid` | No token, an unknown one, or one that does not work. |
| 403 | `forbidden` | No permission of the token gives this resource and action. |
| 404 | `not_found` | No such object, or it is outside the token's permissions or its owner's zone. |
| 405 | `method_not_allowed` | The method is not taken here; the `Allow` header names the ones that are. |
| 409 | `conflict` | The state does not allow the change: the key is already being revoked, the user has no plan, the key limit is reached. |
| 429 | `too_many_calls` | Over `LAMNEZIA_API_RATE_LIMIT` calls a minute; wait `Retry-After` seconds. |

## Endpoints

All under `/api/v1/`:

| Path | What |
|---|---|
| `token` | The token of the call: name, `scope` (`manage` when any permission manages, else `read`), `permissions`, expiry, owner, `zone` (`whole` or `moderator`). |
| `servers`, `servers/<id>` | State, online, maintenance, paid until, the latest load reading, protocols. |
| `users`, `users/<id>` | Role, status, plan, moderators (`moderator_ids`; `moderator_id` is the first of them), effective limits and usage of the period and in total. |
| `keys`, `keys/<id>` | Owner, server, protocol, status, dates. Filters: `user`, `server`, `status`. |
| `traffic` | Traffic by hour (`period=day`) or by day (`week`, `month`) and per key. Filters: `server`, `user`, `key`. When the traffic permission has an object, name it in a filter. |
| `operations/<id>` | The status of an operation this token started: `pending`, `running`, `succeeded`, `failed`, times and the error. |

Changes (`POST`). Each answers `202` at once with `operation_ids`: the work on the server goes on in
the background, see its outcome at `operations/<id>`.

| Path | Body | What |
|---|---|---|
| `keys` | `source`, `user_id`, `name` | Issue a key to a user on a protocol (`source` from `servers` → `protocols`); the answer has `key_id`. |
| `keys/<id>/revoke` | — | Revoke the key. |
| `users/<id>/extend` | `days` (1–3650) | Extend access; the answer has the new `access_until`. |
| `users/<id>/plan` | `plan_id` | Move the user to a plan you may give. |

Changes through the API go to the audit log the same way as those made in the panel, in the name of
the token's owner.

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
