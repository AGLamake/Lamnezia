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
| `server_up` | A server answers again after an outage (`server_down`). Once per outage, even when no channel heard of `server_down`. | `server` name, `host`, `last_seen` — time of the last answer before the outage or an empty string |
| `awg3_dropped` | The amneziawg kernel module on a server is older than 3.1 and dropped the AmneziaWG 3.1 settings of amnezia-awg2. Once, until the settings are back. | `server` name, `host`, `since` — time of the reading that found it |
| `awg3_restored` | amnezia-awg2 takes the 3.1 settings again after `awg3_dropped`. Once, even when no channel heard of `awg3_dropped`. | `server` name, `host`, `since` — the reading the settings were dropped since |
| `server_load_high` | CPU, memory or disk of a server stayed at or above the threshold in **Settings** for several readings in a row. Repeated while the load lasts, no more often than set. | `server` name, `host`, `resource` (`cpu`, `memory`, `disk`), `percent` — the latest reading, `threshold` — percent, `since` — the first reading above the threshold |
| `server_load_normal` | The load of `server_load_high` stayed below the threshold for as many readings. Once, only when the start was told. | the same as `server_load_high` |
| `interface_lost` | A WireGuard-family container of a server runs, but its VPN interface is gone for several readings in a row; the panel restarts the container. Once, until the interface is back. | `server` name, `host`, `container`, `since` — the first reading without the interface, `attempts` — restarts made |
| `interface_restored` | The interface of `interface_lost` is back. Once. | the same as `interface_lost` |
| `interface_repair_failed` | The restarts after `interface_lost` did not bring the interface back; the panel stops restarting. Once. | the same as `interface_lost` |
| `key_connected` | A new key connected for the first time after its issue; once per key, not after a reissue for the same device. | `user`, `key` — device name, `key_id`, `server`, `url` — the key in the portal |
| `limit_reached` | A user's keys are turned off by a limit. | `user`, `reason`: `traffic`, `total_traffic` or `expired` |
| `access_expiring` | The access term of a user ends within three days. Once per term. | `user`, `until` — date `YYYY-MM-DD` |
| `traffic_low` | A user has used the share of a traffic limit set in **Settings** (80% by default). Once per period of that limit; not sent once the limit is reached. Also sent to the user. | `user`, `percent` — share used, `used`, `limit_bytes` — bytes, `limit` (`of the period`, `of the plan`), `incomplete` — traffic of some keys is not counted now, `note`, `url` — usage page of the portal |
| `keys_changed` | The protocol settings of a server or the server address of a key changed; the user must import their keys again. Also sent to the user. | `user`, `server` — server name |
| `keys_revoked` | The protocol was removed from a server with its backups; the user's keys on it are revoked. Also sent to the user. | `user`, `server` — server name |
| `keys_moved` | The clients of legacy AmneziaWG on a server got new keys on AmneziaWG 3.1; the user must import them. Also sent to the user. | `user`, `server` — server name |
| `keys_updated` | A server joined a group behind a relay, or the relay of its group was installed: the user's keys now carry the relay address and must be imported again, once. Also sent to the user. | `user`, `server` — server name |
| `short_id_changed` | The Reality shortId of XRay on a server changed (for example the protocol was set up anew in the Amnezia app); the keys the user added to the app no longer connect and must be imported again. Also sent to the user. | `user`, `server` — server name |
| `registration_pending` | A new user waits for approval. | `user`, `email` |
| `sign_in_locked` | Sign-in as one name from one address is locked after too many wrong passwords. Once per lockout. The password is never sent. | `user` — the name typed in (may not exist), `address`, `failures` — the limit, `minutes` — how long the lockout lasts |
| `ticket_created` | A user opened a support request. | `user`, `number` — request number, `type`, `subject` |
| `chat_message` | Messages in the support chat; those of one user within two minutes come as one. | `user`, `number` — chat number, `text` — the messages, one a line; `count` — how many, when more than one |
| `incident_opened` | Users report that a server does not work. | `server`, `reports` — number of reports |
| `incident_closed` | The incident is closed. | `server` |
| `suggestion_created` | A user suggested a link or a text for the guides. | `user`, `kind` (`Link to an article`, `My own text`), `title` |
| `payment_received` | A user paid for a plan ([Billing plugins](Billing-plugins.md)). | `user`, `plan`, `amount`, `currency`, `provider` — payment method key |
| `relay_switched` | The relay of a group sends its clients to another member: the previous one stopped answering, or an administrator switched it for maintenance. | `group`, `relay` — relay server name, `from`, `to` — names of the previous and the new member |
| `member_unreachable` | The relay stopped reaching a member of a group. Once until the member answers again. | `group`, `relay`, `server` — member name |
| `group_unreachable` | The relay reaches no member of a group: its clients have no connection. | `group`, `relay` |
| `relay_down` | The panel has not read the relay state for two monitoring intervals: the relay or its agent does not answer. Once until a reading succeeds. | `relay`, `host`, `last_seen` — time of the last reading or an empty string |
| `member_lagging` | A group member has missed key changes longer than `LAMNEZIA_GROUP_LAG_ALERT`. Once per lag. | `group`, `server`, `since` — when it started to lag, `reason` — why: `unreachable` (during a change), `missed` (a change of the keys or the settings), `drift` (files differ from the leading server), `stayed` (after a failed leave); missing for a member that lags since an older release |
| `member_reachable` | The relay reaches again a member of a group it was told unreachable (`member_unreachable`). Once per outage. | `group`, `relay`, `server` — member name |
| `group_reachable` | The relay reaches again a member of a group that had none (`group_unreachable`): its clients have a connection. Once per outage. | `group`, `relay` |
| `relay_up` | The panel reads the state of a relay again after an outage (`relay_down`). Once per outage, even when no channel heard of `relay_down`. | `relay`, `host`, `last_seen` — time of the last reading before the outage or an empty string |
| `member_caught_up` | A lagging member (`member_lagging`) has the files of its group again. Once per lag, even when no channel heard of `member_lagging`. | `group`, `server`, `since` — when it started to lag |
| `split_list_stale` | A network list source of a split group did not give a list the panel takes: it did not download, did not read or has less than half of the networks before. The relay keeps the list it has. Once a day while the source fails. | `group`, `source` — host of the source address, `days` — days since the last good list, `error` |
| `split_emergency` | The relay agent moved a split group to forwarding by itself: the uplink fails on every member while a member answers the usual probe. | `group`, `relay`, `server` — the member it forwards to, or `null` |
| `split_restored` | The uplink works again and the agent moved the group back to split routing. | `group`, `relay` |
| `staff_report` | The scheduled report (**Settings → Report to the staff**), or **Send now**. The whole panel. | `period` — `daily` or `weekly`, `since` — start of the period (local `YYYY-MM-DD HH:MM`), `servers_down`, `relays_down`, `groups_down` — names, `traffic` — bytes, `top_keys` — up to five `[key name, bytes]`, `access_ending` — `"user YYYY-MM-DD"`, `failed_operations`, `backups_oldest` and `archive_at` — local time or `null` without a copy, `applications`, `new_requests`, `url` — the dashboard |
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
    {"if": {"properties": {"event": {"const": "server_up"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/server_up"}}}},
    {"if": {"properties": {"event": {"const": "awg3_dropped"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/awg3_dropped"}}}},
    {"if": {"properties": {"event": {"const": "awg3_restored"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/awg3_restored"}}}},
    {"if": {"properties": {"event": {"const": "server_load_high"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/server_load_high"}}}},
    {"if": {"properties": {"event": {"const": "server_load_normal"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/server_load_normal"}}}},
    {"if": {"properties": {"event": {"const": "interface_lost"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/interface_lost"}}}},
    {"if": {"properties": {"event": {"const": "interface_restored"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/interface_restored"}}}},
    {"if": {"properties": {"event": {"const": "interface_repair_failed"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/interface_repair_failed"}}}},
    {"if": {"properties": {"event": {"const": "key_connected"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/key_connected"}}}},
    {"if": {"properties": {"event": {"const": "limit_reached"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/limit_reached"}}}},
    {"if": {"properties": {"event": {"const": "access_expiring"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/access_expiring"}}}},
    {"if": {"properties": {"event": {"const": "traffic_low"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/traffic_low"}}}},
    {"if": {"properties": {"event": {"const": "keys_changed"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/keys_changed"}}}},
    {"if": {"properties": {"event": {"const": "keys_revoked"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/keys_revoked"}}}},
    {"if": {"properties": {"event": {"const": "keys_moved"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/keys_moved"}}}},
    {"if": {"properties": {"event": {"const": "keys_updated"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/keys_updated"}}}},
    {"if": {"properties": {"event": {"const": "short_id_changed"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/short_id_changed"}}}},
    {"if": {"properties": {"event": {"const": "registration_pending"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/registration_pending"}}}},
    {"if": {"properties": {"event": {"const": "sign_in_locked"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/sign_in_locked"}}}},
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
    {"if": {"properties": {"event": {"const": "member_reachable"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/member_reachable"}}}},
    {"if": {"properties": {"event": {"const": "group_reachable"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/group_reachable"}}}},
    {"if": {"properties": {"event": {"const": "relay_up"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/relay_up"}}}},
    {"if": {"properties": {"event": {"const": "member_caught_up"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/member_caught_up"}}}},
    {"if": {"properties": {"event": {"const": "split_list_stale"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/split_list_stale"}}}},
    {"if": {"properties": {"event": {"const": "split_emergency"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/split_emergency"}}}},
    {"if": {"properties": {"event": {"const": "split_restored"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/split_restored"}}}},
    {"if": {"properties": {"event": {"const": "staff_report"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/staff_report"}}}},
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
    "server_up": {
      "type": "object",
      "required": ["server", "host", "last_seen"],
      "properties": {
        "server": {"type": "string"},
        "host": {"type": "string"},
        "last_seen": {"anyOf": [{"type": "string", "format": "date-time"}, {"const": ""}]}
      }
    },
    "awg3_dropped": {
      "type": "object",
      "required": ["server", "host", "since"],
      "properties": {
        "server": {"type": "string"},
        "host": {"type": "string"},
        "since": {"type": "string", "format": "date-time"}
      }
    },
    "awg3_restored": {
      "type": "object",
      "required": ["server", "host", "since"],
      "properties": {
        "server": {"type": "string"},
        "host": {"type": "string"},
        "since": {"type": "string", "format": "date-time"}
      }
    },
    "server_load_high": {
      "type": "object",
      "required": ["server", "host", "resource", "percent", "threshold", "since"],
      "properties": {
        "server": {"type": "string"}, "host": {"type": "string"},
        "resource": {"enum": ["cpu", "memory", "disk"]},
        "percent": {"type": "integer"}, "threshold": {"type": "integer"},
        "since": {"type": "string", "format": "date-time"}
      }
    },
    "server_load_normal": {
      "type": "object",
      "required": ["server", "host", "resource", "percent", "threshold", "since"],
      "properties": {
        "server": {"type": "string"}, "host": {"type": "string"},
        "resource": {"enum": ["cpu", "memory", "disk"]},
        "percent": {"type": "integer"}, "threshold": {"type": "integer"},
        "since": {"type": "string", "format": "date-time"}
      }
    },
    "interface_lost": {
      "type": "object",
      "required": ["server", "host", "container", "since", "attempts"],
      "properties": {
        "server": {"type": "string"}, "host": {"type": "string"},
        "container": {"type": "string"},
        "since": {"type": "string", "format": "date-time"},
        "attempts": {"type": "integer"}
      }
    },
    "interface_restored": {
      "type": "object",
      "required": ["server", "host", "container", "since", "attempts"],
      "properties": {
        "server": {"type": "string"}, "host": {"type": "string"},
        "container": {"type": "string"},
        "since": {"type": "string", "format": "date-time"},
        "attempts": {"type": "integer"}
      }
    },
    "interface_repair_failed": {
      "type": "object",
      "required": ["server", "host", "container", "since", "attempts"],
      "properties": {
        "server": {"type": "string"}, "host": {"type": "string"},
        "container": {"type": "string"},
        "since": {"type": "string", "format": "date-time"},
        "attempts": {"type": "integer"}
      }
    },
    "key_connected": {
      "type": "object",
      "required": ["user", "key", "key_id", "server", "url"],
      "properties": {
        "user": {"type": "string"}, "key": {"type": "string"},
        "key_id": {"type": "integer"}, "server": {"type": "string"},
        "url": {"type": "string", "format": "uri"}
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
    "traffic_low": {
      "type": "object",
      "required": ["user", "percent", "used", "limit_bytes", "limit", "incomplete", "url"],
      "properties": {
        "user": {"type": "string"}, "percent": {"type": "integer"},
        "used": {"type": "integer"}, "limit_bytes": {"type": "integer"},
        "limit": {"enum": ["of the period", "of the plan"]}, "incomplete": {"type": "boolean"},
        "note": {"type": "string"}, "url": {"type": "string", "format": "uri"}
      }
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
    "short_id_changed": {
      "type": "object",
      "required": ["user", "server"],
      "properties": {"user": {"type": "string"}, "server": {"type": "string"}}
    },
    "registration_pending": {
      "type": "object",
      "required": ["user", "email"],
      "properties": {"user": {"type": "string"}, "email": {"type": "string"}}
    },
    "sign_in_locked": {
      "type": "object",
      "required": ["user", "address", "failures", "minutes"],
      "properties": {
        "user": {"type": "string"}, "address": {"type": "string"},
        "failures": {"type": "integer"}, "minutes": {"type": "integer"}
      }
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
      "properties": {"group": {"type": "string"}, "server": {"type": "string"}, "since": {"type": "string", "format": "date-time"}, "reason": {"enum": ["unreachable", "missed", "drift", "stayed"]}}
    },
    "member_reachable": {
      "type": "object",
      "required": ["group", "relay", "server"],
      "properties": {"group": {"type": "string"}, "relay": {"type": "string"}, "server": {"type": "string"}}
    },
    "group_reachable": {
      "type": "object",
      "required": ["group", "relay"],
      "properties": {"group": {"type": "string"}, "relay": {"type": "string"}}
    },
    "relay_up": {
      "type": "object",
      "required": ["relay", "host", "last_seen"],
      "properties": {"relay": {"type": "string"}, "host": {"type": "string"}, "last_seen": {"anyOf": [{"type": "string", "format": "date-time"}, {"const": ""}]}}
    },
    "member_caught_up": {
      "type": "object",
      "required": ["group", "server", "since"],
      "properties": {"group": {"type": "string"}, "server": {"type": "string"}, "since": {"type": "string", "format": "date-time"}}
    },
    "split_list_stale": {
      "type": "object",
      "required": ["group", "source", "days", "error"],
      "properties": {"group": {"type": "string"}, "source": {"type": "string"}, "days": {"type": "integer", "minimum": 1}, "error": {"type": "string"}}
    },
    "split_emergency": {
      "type": "object",
      "required": ["group", "relay", "server"],
      "properties": {"group": {"type": "string"}, "relay": {"type": "string"}, "server": {"type": ["string", "null"]}}
    },
    "split_restored": {
      "type": "object",
      "required": ["group", "relay"],
      "properties": {"group": {"type": "string"}, "relay": {"type": "string"}}
    },
    "staff_report": {
      "type": "object",
      "required": ["period", "since", "servers_down", "relays_down", "groups_down", "traffic",
                   "top_keys", "access_ending", "failed_operations", "backups_oldest",
                   "archive_at", "applications", "new_requests", "url"],
      "properties": {
        "period": {"enum": ["daily", "weekly"]},
        "since": {"type": "string"},
        "servers_down": {"type": "array", "items": {"type": "string"}},
        "relays_down": {"type": "array", "items": {"type": "string"}},
        "groups_down": {"type": "array", "items": {"type": "string"}},
        "traffic": {"type": "integer"},
        "top_keys": {
          "type": "array",
          "items": {"type": "array", "prefixItems": [{"type": "string"}, {"type": "integer"}]}
        },
        "access_ending": {"type": "array", "items": {"type": "string"}},
        "failed_operations": {"type": "integer"},
        "backups_oldest": {"type": ["string", "null"]},
        "archive_at": {"type": ["string", "null"]},
        "applications": {"type": "integer"},
        "new_requests": {"type": "integer"},
        "url": {"type": "string"}
      }
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
