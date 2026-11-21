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
| `server_up` | Сервер после сбоя (`server_down`) снова отвечает. Один раз за сбой, даже если `server_down` не получил ни один канал. | `server` — имя, `host`, `last_seen` — время последнего ответа до сбоя или пустая строка |
| `awg3_dropped` | Модуль ядра amneziawg на сервере старше 3.1 и отбросил параметры AmneziaWG 3.1 у amnezia-awg2. Один раз, пока параметры не вернутся. | `server` — имя, `host`, `since` — время замера, в котором это замечено |
| `awg3_restored` | amnezia-awg2 снова принимает параметры 3.1 после `awg3_dropped`. Один раз, даже если `awg3_dropped` не получил ни один канал. | `server` — имя, `host`, `since` — с какого замера параметры были отброшены |
| `server_load_high` | Процессор, память или диск сервера несколько замеров подряд не ниже порога из **Настроек**. Повторяется, пока нагрузка держится, не чаще заданного. | `server` — имя, `host`, `resource` (`cpu`, `memory`, `disk`), `percent` — последний замер, `threshold` — порог в процентах, `since` — первый замер выше порога |
| `server_load_normal` | Нагрузка из `server_load_high` столько же замеров ниже порога. Один раз и только если о начале сообщили. | как у `server_load_high` |
| `interface_lost` | Контейнер WireGuard-семейства на сервере работает, а его VPN-интерфейса нет несколько замеров подряд; панель перезапускает контейнер. Один раз, пока интерфейс не вернётся. | `server` — имя, `host`, `container`, `since` — первый замер без интерфейса, `attempts` — сделанные перезапуски |
| `interface_restored` | Интерфейс из `interface_lost` вернулся. Один раз. | как у `interface_lost` |
| `interface_repair_failed` | Перезапуски после `interface_lost` не вернули интерфейс; панель больше не перезапускает. Один раз. | как у `interface_lost` |
| `key_connected` | Новый ключ впервые подключился после выдачи; один раз на ключ, без повтора после перевыдачи того же устройства. | `user`, `key` — имя устройства, `key_id`, `server`, `url` — ключ в портале |
| `limit_reached` | Ключи пользователя отключены по лимиту. | `user`, `reason`: `traffic`, `total_traffic` или `expired` |
| `access_expiring` | Срок доступа пользователя кончается в ближайшие три дня. Один раз на срок. | `user`, `until` — дата `YYYY-MM-DD` |
| `traffic_low` | Пользователь израсходовал долю лимита трафика, заданную в **Настройках** (по умолчанию 80%). Один раз за период этого лимита; после достижения лимита не приходит. Приходит и самому пользователю. | `user`, `percent` — израсходованная доля, `used`, `limit_bytes` — байты, `limit` (`of the period`, `of the plan`), `incomplete` — трафик части ключей сейчас не считается, `note`, `url` — страница расхода в портале |
| `keys_changed` | Изменились настройки протокола на сервере или адрес сервера в ключе; пользователю нужно заново импортировать ключи. Приходит и самому пользователю. | `user`, `server` — имя сервера |
| `keys_revoked` | Протокол удалён с сервера вместе с бэкапами; ключи пользователя на нём отозваны. Приходит и самому пользователю. | `user`, `server` — имя сервера |
| `keys_moved` | Клиенты старого AmneziaWG на сервере получили новые ключи на AmneziaWG 3.1; пользователю нужно их импортировать. Приходит и самому пользователю. | `user`, `server` — имя сервера |
| `keys_updated` | Сервер вошёл в группу за ретранслятором или у его группы установлен ретранслятор: ключи пользователя теперь ведут на адрес ретранслятора, их нужно один раз импортировать заново. Приходит и самому пользователю. | `user`, `server` — имя сервера |
| `keys_reissued` | Ключи пользователя на сервере переиздал кто-то другой (персонал, пакетное переиздание, смена настроек группы или протокола): старые больше не подключаются, новые нужно импортировать заново. Один раз за переиздание. Приходит и самому пользователю. | `user`, `server` — сервер или группа и протокол |
| `short_id_changed` | Сменился shortId Reality у XRay на сервере (например, протокол заново настроили в приложении Amnezia); добавленные в приложение ключи пользователя больше не подключаются, их нужно импортировать заново. Приходит и самому пользователю. | `user`, `server` — имя сервера |
| `registration_pending` | Новый пользователь ждёт одобрения. | `user`, `email` |
| `sign_in_locked` | Вход под одним именем с одного адреса заблокирован после слишком многих неверных паролей. Один раз за блокировку. Пароль не передаётся. | `user` — введённое имя (может не существовать), `address`, `failures` — лимит, `minutes` — на сколько минут блокировка |
| `ticket_created` | Пользователь открыл обращение в поддержку. | `user`, `number` — номер обращения, `type`, `subject` |
| `chat_message` | Сообщения в чате поддержки; сообщения одного пользователя за две минуты приходят одним. | `user`, `number` — номер чата, `text` — сообщения, по одному на строку; `count` — сколько их, если больше одного |
| `incident_opened` | Пользователи сообщают, что сервер не работает. | `server`, `reports` — число сообщений |
| `incident_closed` | Инцидент закрыт. | `server` |
| `suggestion_created` | Пользователь предложил ссылку или текст для инструкций. | `user`, `kind` (`Link to an article`, `My own text`), `title` |
| `payment_received` | Пользователь оплатил тариф ([Плагины оплаты](Billing-plugins.md)). | `user`, `plan`, `amount`, `currency`, `provider` — ключ способа оплаты |
| `relay_switched` | Ретранслятор группы отправляет клиентов на другого участника: прежний перестал отвечать или администратор переключил его на время обслуживания. | `group`, `relay` — имя сервера-ретранслятора, `from`, `to` — имена прежнего и нового участника |
| `member_unreachable` | Ретранслятор перестал получать ответ от участника группы. Один раз, пока участник снова не ответит. | `group`, `relay`, `server` — имя участника |
| `group_unreachable` | Ретранслятор не получает ответа ни от одного участника группы: у её клиентов нет связи. | `group`, `relay` |
| `relay_down` | Панель не читает состояние ретранслятора два интервала мониторинга: не отвечает ретранслятор или его агент. Один раз, пока чтение снова не удастся. | `relay`, `host`, `last_seen` — время последнего чтения или пустая строка |
| `member_lagging` | Участник группы пропускает изменения ключей дольше `LAMNEZIA_GROUP_LAG_ALERT`. Один раз за отставание. | `group`, `server`, `since` — с какого момента отстаёт, `reason` — почему: `unreachable` (был недоступен во время изменения), `missed` (пропустил изменение ключей или параметров), `drift` (файлы отличаются от ведущего), `stayed` (остался после неудачного выхода); нет у участника, отстающего с прежней версии |
| `member_reachable` | Ретранслятор снова получает ответ от участника группы, о недоступности которого сообщил (`member_unreachable`). Один раз за сбой. | `group`, `relay`, `server` — имя участника |
| `group_reachable` | У группы, где ни один участник не отвечал (`group_unreachable`), снова есть доступный участник: у её клиентов есть связь. Один раз за сбой. | `group`, `relay` |
| `relay_up` | Панель снова читает состояние ретранслятора после сбоя (`relay_down`). Один раз за сбой, даже если `relay_down` не получил ни один канал. | `relay`, `host`, `last_seen` — время последнего чтения до сбоя или пустая строка |
| `member_caught_up` | Отстававший участник (`member_lagging`) снова получил файлы группы. Один раз за отставание, даже если `member_lagging` не получил ни один канал. | `group`, `server`, `since` — с какого момента отставал |
| `panel_archive_failed` | Операция архива панели не удалась: архив не создан или его копия не дошла до внешнего хранилища. Раз в сутки. | `error` — что пошло не так |
| `split_list_stale` | Источник списка сетей группы в раздельном режиме не дал список, который панель принимает: не скачался, не разобрался или в нём меньше половины прежних сетей. Ретранслятор работает с прежним списком. Раз в сутки, пока источник не обновляется. | `group`, `source` — хост адреса источника, `days` — дней с последнего удачного списка, `error` |
| `split_emergency` | Агент ретранслятора сам перевёл группу из раздельного режима на проброс: канал не работает ни через одного участника, а участник отвечает на обычную проверку. | `group`, `relay`, `server` — участник, на который идёт проброс, или `null` |
| `split_restored` | Канал снова работает, и агент вернул группу в раздельный режим. | `group`, `relay` |
| `staff_report` | Отчёт по расписанию (**Настройки → Отчёт персоналу**) или **Прислать сейчас**. Вся панель. | `period` — `daily` или `weekly`, `since` — начало периода (местное `YYYY-MM-DD HH:MM`), `servers_down`, `relays_down`, `groups_down` — имена, `traffic` — байты, `top_keys` — до пяти `[имя ключа, байты]`, `access_ending` — `"user YYYY-MM-DD"`, `failed_operations`, `backups_oldest` и `archive_at` — местное время или `null`, если копии нет, `applications`, `new_requests`, `url` — главная панели |
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
    {"if": {"properties": {"event": {"const": "keys_reissued"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/keys_reissued"}}}},
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
    {"if": {"properties": {"event": {"const": "panel_archive_failed"}}},
     "then": {"properties": {"data": {"$ref": "#/$defs/panel_archive_failed"}}}},
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
    "keys_reissued": {
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
    "panel_archive_failed": {
      "type": "object",
      "required": ["error"],
      "properties": {"error": {"type": "string"}}
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
