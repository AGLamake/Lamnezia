# Архитектура Lamnezia

Схемы в Mermaid — GitHub рендерит их прямо в репозитории. Текстовое описание решений — [DESIGN.md](DESIGN.md).

## 1. Развёртывание

```mermaid
flowchart LR
    subgraph Users["Браузеры"]
        A[Админ]
        U[Пользователь]
    end

    subgraph Panel["VPS панели — docker compose"]
        C[caddy<br/>HTTPS :443]
        W[web<br/>ASGI: Django + Channels<br/>HTTP + WebSocket]
        WK[worker<br/>Celery: SSH-операции]
        B[beat<br/>расписание]
        BOT[bots — опционально<br/>Telegram · Discord]
        P[(postgres)]
        R[(redis<br/>брокер + кэш)]
    end

    subgraph VPN1["VPN-сервер 1"]
        D1[docker]
        AWG1[amnezia-awg2<br/>awg0.conf + clientsTable]
    end

    subgraph VPN2["VPN-сервер N"]
        D2[docker]
        AWG2[amnezia-awg2]
    end

    GH[GitHub API<br/>releases/latest]
    APP[Приложение Amnezia<br/>у админа — по-прежнему работает]
    NT[Email · Telegram · Discord · Webhook<br/>уведомления админу и пользователям]
    MSG[Telegram / Discord<br/>пользователи и админы в ботах]

    A & U --> C --> W
    W <--> P
    W <--> R
    B --> R --> WK
    WK <--> P
    WK -- SSH + docker exec --> D1 --> AWG1
    WK -- SSH + docker exec --> D2 --> AWG2
    WK -. кэш релизов .-> GH
    APP -. SSH, тот же clientsTable .-> AWG1
    WK -- по правилам маршрутизации --> NT
    MSG <--> BOT
    BOT <--> P
    BOT -- операции с ключами --> R
```

## 2. Слои кода

```mermaid
flowchart TB
    UI["apps/dashboard · apps/portal · apps/guides<br/>views, templates, HTMX, WebSocket consumers"]
    DOM["apps/accounts · apps/plans · apps/keys · apps/servers · apps/protocols<br/>apps/traffic · apps/support · apps/notifications · apps/audit"]
    OPS["apps/operations<br/>журнал фоновых операций, Celery-задачи"]
    SSH["apps/ssh<br/>paramiko, таймауты, sudo, flock, каталог ошибок"]
    CORE["amnezia_core — чистый Python, без Django<br/>awg conf · clientsTable · ключи X25519 · IP · vpn:// · awg show dump"]

    UI --> DOM --> OPS --> SSH
    DOM --> CORE
    OPS --> CORE
```

Правило: `amnezia_core` не импортирует Django и не ходит в сеть. Всё знание о форматах Amnezia — только там.

### Слой интерфейса

```mermaid
flowchart LR
    T["templates/layouts<br/>admin · portal · auth"] --> C["templates/components<br/>+ теги apps/core/templatetags/ui.py"]
    C --> CSS["assets/css/app.css<br/>токены --ln-* и компоненты"]
    T --> JS["static/js/theme.js · ui.js<br/>Alpine CSP + делегированные обработчики"]
    T --> H["HTMX<br/>фрагменты, опрос прогресса"]
```

Страница — серверный HTML. Состояние на клиенте ограничено Alpine-компонентами из `ui.js`; CSP
`script-src 'self'` без `'unsafe-eval'`. Страницы ошибок — `templates/400|403|404|500.html` и
`templates/errors/` (отказ CSRF — `apps/core/errors.csrf_failure`); они не наследуют каркасы и не
зависят от контекстных процессоров.

## 3. Выдача ключа пользователю

```mermaid
sequenceDiagram
    actor U as Пользователь
    participant W as web
    participant Q as worker
    participant S as VPN-сервер

    U->>W: «Получить ключ» (сервер, устройство)
    W->>W: проверка лимитов плана (число ключей, серверы, срок)
    W->>Q: Operation(issue_key)
    W-->>U: страница прогресса (HTMX polling)
    Q->>S: flock /opt/amnezia/.lamnezia.lock
    Q->>S: прочитать awg0.conf, clientsTable
    Q->>Q: amnezia_core: ключи, свободный IP, новый [Peer]
    Q->>S: записать tmp → mv, awg syncconf
    Q->>S: записать clientsTable (+ lamneziaUserId)
    Q->>S: снять lock
    Q->>W: AccessKey(active), AuditEvent
    W-->>U: vpn:// + QR + .conf + ссылка на приложение
```

## 4. Импорт существующего сервера

```mermaid
flowchart TD
    S1[Админ вводит SSH-доступ] --> S2[Проверка: SSH, sudo, docker]
    S2 -->|ошибка| E[Человеческое сообщение + что сделать]
    S2 --> S3[docker ps: найти amnezia-* контейнеры]
    S3 --> S4[Для amnezia-awg2: прочитать awg0.conf,<br/>ключи сервера, clientsTable]
    S4 --> S5[Создать ProtocolInstance source=imported]
    S5 --> S6[Каждый Peer → AccessKey без владельца<br/>имя из clientsTable]
    S6 --> S7[Админ привязывает ключи к пользователям<br/>или отзывает]
```

## 5. Учёт трафика и лимиты

```mermaid
flowchart LR
    B[beat, каждые 5 мин] --> C[awg show all dump]
    C --> D{rx/tx < прошлого?}
    D -->|да: сброс счётчика| E[дельта = текущее]
    D -->|нет| F[дельта = текущее − прошлое]
    E & F --> H[TrafficHourly += дельта]
    H --> L{превышен лимит<br/>или истёк срок?}
    L -->|да| X[отключить пира<br/>status=disabled_limit<br/>уведомить]
    L -->|новый период / продление| Y[включить пира]
```

## 6. «Сервер не работает» → инцидент → уведомления

```mermaid
sequenceDiagram
    actor U as Пользователь
    participant W as web
    participant Q as worker
    participant S as VPN-сервер
    participant N as Каналы (email / Telegram / webhook)
    actor A as Админ

    U->>W: быстрая форма «сервер не работает»
    W->>W: Conversation(ticket, server_down) + авто-контекст
    W->>Q: немедленная проверка сервера
    Q->>S: SSH, docker ps, awg show
    Q->>W: результат проверки → в обращение
    W->>W: есть инцидент по серверу за 30 мин? → добавить, счётчик +1 : создать
    W->>Q: emit(incident_opened | ticket_created)
    Q->>N: по правилам маршрутизации (HMAC для webhook, ретраи)
    N-->>A: уведомление
    A->>W: входящие → инцидент → ответ / закрыть
    W-->>U: ответ в треде (WebSocket) + уведомление, если офлайн
```

## 7. Жизненный цикл ключа

```mermaid
stateDiagram-v2
    [*] --> active: выдан / импортирован
    active --> disabled_limit: лимит трафика или срок
    disabled_limit --> active: новый период / продление
    active --> disabled_manual: админ отключил
    disabled_manual --> active: админ включил
    active --> revoked: отзыв / перевыпуск
    disabled_limit --> revoked
    disabled_manual --> revoked
    revoked --> [*]
```
