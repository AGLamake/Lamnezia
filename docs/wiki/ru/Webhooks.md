# Вебхуки

Канал вида webhook ([Уведомления](Notifications.md)) отправляет каждое назначенное ему событие на
ваш URL в виде JSON, подписанного секретом канала. Эта страница — для тех, кто пишет приёмник.

## Запрос

`POST` на URL канала, `Content-Type: application/json`, UTF-8, с заголовками:

| Заголовок | Значение |
|---|---|
| `X-Lamnezia-Event` | Событие, например `server_down`. |
| `X-Lamnezia-Delivery` | ID события, одинаковый при повторах: по нему отбрасывайте дубли. |
| `X-Lamnezia-Signature` | `sha256=` и HMAC-SHA256 сырого тела с секретом канала, hex в нижнем регистре. |

Тело:

```json
{
  "id": "6f1c2a9e-3b4d-4a8e-9d51-0c7b2e8f4a10",
  "event": "server_down",
  "created_at": "2026-11-03T12:00:00.123456+00:00",
  "data": {"server": "Germany", "host": "203.0.113.7", "last_seen": "2026-11-03T11:57:00+00:00"}
}
```

`id` совпадает с `X-Lamnezia-Delivery`, `created_at` — время события (ISO 8601 с часовым поясом),
`data` зависит от события.

## События

| `event` | Когда | `data` |
|---|---|---|
| `server_down` | Сервер не отвечает два интервала мониторинга. Один раз, пока сервер не вернётся. | `server` — имя, `host`, `last_seen` — время последнего ответа или пустая строка, если ответов не было |
| `limit_reached` | Ключи пользователя отключены по лимиту. | `user`, `reason`: `traffic`, `total_traffic` или `expired` |
| `access_expiring` | Срок доступа пользователя кончается в ближайшие три дня. Один раз на срок. | `user`, `until` — дата `YYYY-MM-DD` |
| `keys_changed` | Изменились настройки протокола на сервере; пользователю нужно заново импортировать ключи. Приходит и самому пользователю. | `user`, `server` — имя сервера |
| `keys_revoked` | Протокол удалён с сервера вместе с бэкапами; ключи пользователя на нём отозваны. Приходит и самому пользователю. | `user`, `server` — имя сервера |
| `keys_moved` | Клиенты старого AmneziaWG на сервере получили новые ключи на AmneziaWG 3.1; пользователю нужно их импортировать. Приходит и самому пользователю. | `user`, `server` — имя сервера |
| `keys_updated` | Сервер вошёл в группу за ретранслятором или у его группы установлен ретранслятор: ключи пользователя теперь ведут на адрес ретранслятора, их нужно один раз импортировать заново. Приходит и самому пользователю. | `user`, `server` — имя сервера |
| `registration_pending` | Новый пользователь ждёт одобрения. | `user`, `email` |
| `ticket_created` | Пользователь открыл обращение в поддержку. | `user`, `number` — номер обращения, `type`, `subject` |
| `chat_message` | Сообщения в чате поддержки; сообщения одного пользователя за две минуты приходят одним. | `user`, `number` — номер чата, `text` — сообщения, по одному на строку; `count` — сколько их, если больше одного |
| `incident_opened` | Пользователи сообщают, что сервер не работает. | `server`, `reports` — число сообщений |
| `incident_closed` | Инцидент закрыт. | `server` |
| `suggestion_created` | Пользователь предложил ссылку или текст для инструкций. | `user`, `kind` (`Link to an article`, `My own text`), `title` |
| `payment_received` | Пользователь оплатил тариф ([Плагины оплаты](Billing-plugins.md)). | `user`, `plan`, `amount`, `currency`, `provider` — ключ способа оплаты |
| `relay_switched` | Ретранслятор группы отправляет клиентов на другого участника: прежний перестал отвечать. | `group`, `relay` — имя сервера-ретранслятора, `from`, `to` — имена прежнего и нового участника |
| `member_unreachable` | Ретранслятор перестал получать ответ от участника группы. Один раз, пока участник снова не ответит. | `group`, `relay`, `server` — имя участника |
| `group_unreachable` | Ретранслятор не получает ответа ни от одного участника группы: у её клиентов нет связи. | `group`, `relay` |
| `relay_down` | Панель не читает состояние ретранслятора два интервала мониторинга: не отвечает ретранслятор или его агент. Один раз, пока чтение снова не удастся. | `relay`, `host`, `last_seen` — время последнего чтения или пустая строка |
| `member_lagging` | Участник группы пропускает изменения ключей дольше `LAMNEZIA_GROUP_LAG_ALERT`. Один раз за отставание. | `group`, `server`, `since` — с какого момента отстаёт |
| `test` | **Отправить тест** на странице канала. | `channel` — название канала |

Сообщения в чате начнут приходить, когда появится чат поддержки. Могут добавиться новые
события и новые поля `data`: всё незнакомое пропускайте.

## JSON-схема

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

## Подпись

Посчитайте HMAC-SHA256 **сырых байтов тела** с секретом канала, допишите спереди `sha256=` и
сравните с `X-Lamnezia-Signature` за постоянное время. Проверяйте до разбора JSON: разобранное и
заново собранное тело — это другие байты и другая подпись. Запрос с неверной подписью пришёл не от
панели: ответьте `401` и не обрабатывайте его.

## Повторы и ответы

- Ответьте любым `2xx` в течение 10 секунд. Тело ответа не читается.
- Нет соединения, таймаут, `408`, `425`, `429` и `5xx` повторяются через 30 секунд, затем через 1, 2
  и 4 минуты: всего 5 попыток. Любой другой статус — сразу отказ.
- Повтор приходит с тем же телом, `id` и подписью. Храните обработанные `id` и пропускайте дубли.
- Долгую работу делайте после ответа, чтобы панель не ушла в таймаут и повтор.

Каждая попытка и её ошибка видны в **Админка → Уведомления → Журнал**.

## Приёмник на Python

Только стандартная библиотека. Запуск: `LAMNEZIA_WEBHOOK_SECRET=... python receiver.py`, слушает
порт 8080.

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

## Приёмник на Node.js

Без пакетов. Запуск: `LAMNEZIA_WEBHOOK_SECRET=... node receiver.js`, слушает порт 8080.

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

1. Добавьте узел **Webhook**: метод `POST`, **Respond** — *Immediately*, в опциях включите
   **Raw Body**. Скопируйте его production URL в канал.
2. Добавьте узел **Code** (*Run Once for All Items*), который проверяет подпись и передаёт событие
   дальше:

```javascript
const crypto = require("crypto");

const secret = "секрет вашего канала";
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

В self-hosted n8n разрешите модуль для узла Code: `NODE_FUNCTION_ALLOW_BUILTIN=crypto`.

3. Узлом **Switch** разведите события по `{{ $json.event }}` и отправьте их, куда нужно.

## Discord

Чтобы просто публиковать события в канал Discord, приёмник не нужен: добавьте канал вида
**Discord** с webhook URL из Discord ([Уведомления](Notifications.md)). n8n или свой приёмник нужны,
если хочется своего формата. Например, после узла Code выше узел **HTTP Request** отправляет на
webhook URL Discord JSON:

```json
{
  "username": "Lamnezia",
  "content": "**{{ $json.event }}**: {{ JSON.stringify($json.data) }}"
}
```
