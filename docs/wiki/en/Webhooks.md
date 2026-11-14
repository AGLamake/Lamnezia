# Webhooks

A webhook channel ([Notifications](Notifications.md)) posts every routed event to your URL as JSON
signed with the channel secret. This page is for whoever writes the receiver.

## Request

`POST` to the channel URL, `Content-Type: application/json`, UTF-8, with the headers:

| Header | Value |
|---|---|
| `X-Lamnezia-Event` | Event, e.g. `server_down`. |
| `X-Lamnezia-Delivery` | Event ID, the same on every retry: use it to drop repeats. |
| `X-Lamnezia-Signature` | `sha256=` and the HMAC-SHA256 of the raw body with the channel secret, lowercase hex. |

Body:

```json
{
  "id": "6f1c2a9e-3b4d-4a8e-9d51-0c7b2e8f4a10",
  "event": "server_down",
  "created_at": "2026-11-03T12:00:00.123456+00:00",
  "data": {"server": "Germany", "host": "203.0.113.7", "last_seen": "2026-11-03T11:57:00+00:00"}
}
```

`id` equals `X-Lamnezia-Delivery`, `created_at` is when the event happened (ISO 8601 with the time
zone), `data` depends on the event.

## Events

| `event` | When | `data` |
|---|---|---|
| `server_down` | A server has not answered for two monitoring intervals. Sent once until the server comes back. | `server` name, `host`, `last_seen` — time of the last answer or an empty string if it never answered |
| `limit_reached` | A user's keys are turned off by a limit. | `user`, `reason`: `traffic`, `total_traffic` or `expired` |
| `access_expiring` | The access term of a user ends within three days. Once per term. | `user`, `until` — date `YYYY-MM-DD` |
| `keys_changed` | The protocol settings of a server changed; the user must import their keys again. Also sent to the user. | `user`, `server` — server name |
| `keys_revoked` | The protocol was removed from a server with its backups; the user's keys on it are revoked. Also sent to the user. | `user`, `server` — server name |
| `keys_moved` | The clients of legacy AmneziaWG on a server got new keys on AmneziaWG 3.1; the user must import them. Also sent to the user. | `user`, `server` — server name |
| `keys_updated` | A server joined a group behind a relay, or the relay of its group was installed: the user's keys now carry the relay address and must be imported again, once. Also sent to the user. | `user`, `server` — server name |
| `registration_pending` | A new user waits for approval. | `user`, `email` |
| `ticket_created` | A user opened a support request. | `user`, `number` — request number, `type`, `subject` |
| `chat_message` | Messages in the support chat; those of one user within two minutes come as one. | `user`, `number` — chat number, `text` — the messages, one a line; `count` — how many, when more than one |
| `incident_opened` | Users report that a server does not work. | `server`, `reports` — number of reports |
| `incident_closed` | The incident is closed. | `server` |
| `suggestion_created` | A user suggested a link or a text for the guides. | `user`, `kind` (`Link to an article`, `My own text`), `title` |
| `payment_received` | A user paid for a plan ([Billing plugins](Billing-plugins.md)). | `user`, `plan`, `amount`, `currency`, `provider` — payment method key |
| `relay_switched` | The relay of a group sends its clients to another member: the previous one stopped answering. | `group`, `relay` — relay server name, `from`, `to` — names of the previous and the new member |
| `member_unreachable` | The relay stopped reaching a member of a group. Once until the member answers again. | `group`, `relay`, `server` — member name |
| `group_unreachable` | The relay reaches no member of a group: its clients have no connection. | `group`, `relay` |
| `relay_down` | The panel has not read the relay state for two monitoring intervals: the relay or its agent does not answer. Once until a reading succeeds. | `relay`, `host`, `last_seen` — time of the last reading or an empty string |
| `member_lagging` | A group member has missed key changes longer than `LAMNEZIA_GROUP_LAG_ALERT`. Once per lag. | `group`, `server`, `since` — when it started to lag |
| `test` | **Send test** on the channel page. | `channel` — channel name |

Chat messages are sent once the support chat is available. New events and new `data` fields
may be added: ignore what you do not know.

## JSON schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Lamnezia webhook",
  "type": "object",
  "required": ["id", "event", "created_at", "data"],
  "properties": {
    "id": {"type": "string", "format": "uuid"},
    "event": {"type": "string"},
    "created_at": {"type": "string", "format": "date-time"},
    "data": {"type": "object"}
  },
  "allOf": [
    {"if": {"properties": {"event": {"const": "server_down"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/server_down"}}}},
    {"if": {"properties": {"event": {"const": "limit_reached"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/limit_reached"}}}},
    {"if": {"properties": {"event": {"const": "access_expiring"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/access_expiring"}}}},
    {"if": {"properties": {"event": {"const": "keys_changed"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/keys_changed"}}}},
    {"if": {"properties": {"event": {"const": "keys_revoked"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/keys_revoked"}}}},
    {"if": {"properties": {"event": {"const": "keys_moved"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/keys_moved"}}}},
    {"if": {"properties": {"event": {"const": "keys_updated"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/keys_updated"}}}},
    {"if": {"properties": {"event": {"const": "registration_pending"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/registration_pending"}}}},
    {"if": {"properties": {"event": {"const": "ticket_created"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/ticket_created"}}}},
    {"if": {"properties": {"event": {"const": "chat_message"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/chat_message"}}}},
    {"if": {"properties": {"event": {"const": "incident_opened"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/incident_opened"}}}},
    {"if": {"properties": {"event": {"const": "incident_closed"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/incident_closed"}}}},
    {"if": {"properties": {"event": {"const": "suggestion_created"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/suggestion_created"}}}},
    {"if": {"properties": {"event": {"const": "payment_received"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/payment_received"}}}},
    {"if": {"properties": {"event": {"const": "relay_switched"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/relay_switched"}}}},
    {"if": {"properties": {"event": {"const": "member_unreachable"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/member_unreachable"}}}},
    {"if": {"properties": {"event": {"const": "group_unreachable"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/group_unreachable"}}}},
    {"if": {"properties": {"event": {"const": "relay_down"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/relay_down"}}}},
    {"if": {"properties": {"event": {"const": "member_lagging"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/member_lagging"}}}},
    {"if": {"properties": {"event": {"const": "test"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/test"}}}}
  ],
  "$defs": {
    "server_down": {
      "type": "object",
      "required": ["server", "host", "last_seen"],
      "properties": {
        "server": {"type": "string"},
        "host": {"type": "string"},
        "last_seen": {"anyOf": [{"type": "string", "format": "date-time"}, {"const": ""}]}
      }
    },
    "limit_reached": {
      "type": "object",
      "required": ["user", "reason"],
      "properties": {
        "user": {"type": "string"},
        "reason": {"enum": ["traffic", "total_traffic", "expired"]}
      }
    },
    "access_expiring": {
      "type": "object",
      "required": ["user", "until"],
      "properties": {"user": {"type": "string"}, "until": {"type": "string", "format": "date"}}
    },
    "keys_changed": {
      "type": "object",
      "required": ["user", "server"],
      "properties": {"user": {"type": "string"}, "server": {"type": "string"}}
    },
    "keys_revoked": {
      "type": "object",
      "required": ["user", "server"],
      "properties": {"user": {"type": "string"}, "server": {"type": "string"}}
    },
    "keys_moved": {
      "type": "object",
      "required": ["user", "server"],
      "properties": {"user": {"type": "string"}, "server": {"type": "string"}}
    },
    "keys_updated": {
      "type": "object",
      "required": ["user", "server"],
      "properties": {"user": {"type": "string"}, "server": {"type": "string"}}
    },
    "registration_pending": {
      "type": "object",
      "required": ["user", "email"],
      "properties": {"user": {"type": "string"}, "email": {"type": "string"}}
    },
    "ticket_created": {
      "type": "object",
      "required": ["user", "number", "type", "subject"],
      "properties": {
        "user": {"type": "string"},
        "number": {"type": "integer"},
        "type": {"type": "string"},
        "subject": {"type": "string"}
      }
    },
    "chat_message": {
      "type": "object",
      "required": ["user", "number", "text"],
      "properties": {
        "user": {"type": "string"},
        "number": {"type": "integer"},
        "text": {"type": "string"},
        "count": {"type": "integer"}
      }
    },
    "incident_opened": {
      "type": "object",
      "required": ["server", "reports"],
      "properties": {"server": {"type": "string"}, "reports": {"type": "integer"}}
    },
    "incident_closed": {
      "type": "object",
      "required": ["server"],
      "properties": {"server": {"type": "string"}}
    },
    "suggestion_created": {
      "type": "object",
      "required": ["user", "kind", "title"],
      "properties": {
        "user": {"type": "string"},
        "kind": {"type": "string"},
        "title": {"type": "string"}
      }
    },
    "payment_received": {
      "type": "object",
      "required": ["user", "plan", "amount", "currency", "provider"],
      "properties": {
        "user": {"type": "string"},
        "plan": {"type": "string"},
        "amount": {"type": "string"},
        "currency": {"type": "string"},
        "provider": {"type": "string"}
      }
    },
    "relay_switched": {
      "type": "object",
      "required": ["group", "relay", "from", "to"],
      "properties": {"group": {"type": "string"}, "relay": {"type": "string"}, "from": {"type": "string"}, "to": {"type": "string"}}
    },
    "member_unreachable": {
      "type": "object",
      "required": ["group", "relay", "server"],
      "properties": {"group": {"type": "string"}, "relay": {"type": "string"}, "server": {"type": "string"}}
    },
    "group_unreachable": {
      "type": "object",
      "required": ["group", "relay"],
      "properties": {"group": {"type": "string"}, "relay": {"type": "string"}}
    },
    "relay_down": {
      "type": "object",
      "required": ["relay", "host", "last_seen"],
      "properties": {"relay": {"type": "string"}, "host": {"type": "string"}, "last_seen": {"anyOf": [{"type": "string", "format": "date-time"}, {"const": ""}]}}
    },
    "member_lagging": {
      "type": "object",
      "required": ["group", "server", "since"],
      "properties": {"group": {"type": "string"}, "server": {"type": "string"}, "since": {"type": "string", "format": "date-time"}}
    },
    "test": {
      "type": "object",
      "required": ["channel"],
      "properties": {"channel": {"type": "string"}}
    }
  }
}
```

## Signature

Compute HMAC-SHA256 of the **raw body bytes** with the channel secret, add `sha256=` in front and
compare with `X-Lamnezia-Signature` in constant time. Check before parsing the JSON: a parsed and
re-serialized body has other bytes and another signature. A request with a wrong signature did not
come from the panel: answer `401` and ignore it.

## Retries and answers

- Answer with any `2xx` within 10 seconds. The response body is ignored.
- No connection, a timeout, `408`, `425`, `429` and `5xx` are retried after 30 seconds, then 1, 2
  and 4 minutes: 5 attempts in all. Any other status fails the delivery at once.
- A retry carries the same body, `id` and signature. Keep the `id` values you have handled and skip
  repeats.
- Do the long work after answering, so the panel does not time out and retry.

Every attempt and its error are in **Admin → Notifications → Log**.

## Receiver in Python

Standard library only. Run `LAMNEZIA_WEBHOOK_SECRET=... python receiver.py`; it listens on port 8080.

```python
import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


def is_from_lamnezia(secret: str, body: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class Receiver(BaseHTTPRequestHandler):
    seen: set[str] = set()

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        signature = self.headers.get("X-Lamnezia-Signature", "")
        if not is_from_lamnezia(os.environ["LAMNEZIA_WEBHOOK_SECRET"], body, signature):
            self.send_response(401)
            self.end_headers()
            return
        event = json.loads(body)
        if event["id"] not in self.seen:
            self.seen.add(event["id"])
            print(event["event"], event["data"])
        self.send_response(204)
        self.end_headers()


if __name__ == "__main__":
    HTTPServer(("", 8080), Receiver).serve_forever()
```

## Receiver in Node.js

No packages needed. Run `LAMNEZIA_WEBHOOK_SECRET=... node receiver.js`; it listens on port 8080.

```javascript
const crypto = require("node:crypto");
const http = require("node:http");

function isFromLamnezia(secret, body, signature) {
  const expected = Buffer.from(
    "sha256=" + crypto.createHmac("sha256", secret).update(body).digest("hex"),
  );
  const given = Buffer.from(signature || "");
  return given.length === expected.length && crypto.timingSafeEqual(given, expected);
}

const seen = new Set();

const server = http.createServer((request, response) => {
  const chunks = [];
  request.on("data", (chunk) => chunks.push(chunk));
  request.on("end", () => {
    const body = Buffer.concat(chunks);
    const signature = request.headers["x-lamnezia-signature"];
    if (!isFromLamnezia(process.env.LAMNEZIA_WEBHOOK_SECRET, body, signature)) {
      response.writeHead(401).end();
      return;
    }
    const event = JSON.parse(body);
    if (!seen.has(event.id)) {
      seen.add(event.id);
      console.log(event.event, event.data);
    }
    response.writeHead(204).end();
  });
});

module.exports = { isFromLamnezia };

if (require.main === module) {
  server.listen(8080);
}
```

## n8n

1. Add a **Webhook** node: method `POST`, **Respond** — *Immediately*, and in the options turn on
   **Raw Body**. Copy its production URL into the channel.
2. Add a **Code** node (*Run Once for All Items*) that checks the signature and passes the event
   on:

```javascript
const crypto = require("crypto");

const secret = "your channel secret";
const item = $input.first();
const body = Buffer.from(item.binary.data.data, "base64");
const expected = "sha256=" + crypto.createHmac("sha256", secret).update(body).digest("hex");
const given = item.json.headers["x-lamnezia-signature"] || "";

if (given.length !== expected.length ||
    !crypto.timingSafeEqual(Buffer.from(given), Buffer.from(expected))) {
  throw new Error("Not from Lamnezia");
}
return [{ json: JSON.parse(body.toString("utf8")) }];
```

On self-hosted n8n allow the module in the Code node: `NODE_FUNCTION_ALLOW_BUILTIN=crypto`.

3. Branch on `{{ $json.event }}` with a **Switch** node and send the events where you need them.

## Discord

To simply post events to a Discord channel, you do not need a webhook receiver: add a **Discord**
channel with the Discord webhook URL ([Notifications](Notifications.md)). Use n8n or your own
receiver when you want your own format. For example, after the Code node above, an **HTTP Request**
node posts to the Discord webhook URL with the JSON body:

```json
{
  "username": "Lamnezia",
  "content": "**{{ $json.event }}**: {{ JSON.stringify($json.data) }}"
}
```
