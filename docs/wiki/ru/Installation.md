# Установка

Панель ставится на VPS через docker compose. Серверы Amnezia VPN, которыми она управляет, могут быть
другими машинами: панель ходит на них по SSH. Ставить панель на тот же сервер, что и VPN, тоже можно.

## 1. Что нужно

**VPS для панели**

| | Минимум | Рекомендуется |
|---|---|---|
| CPU | 1 ядро | 2 ядра |
| RAM | 1 ГБ | 2 ГБ |
| Диск | 10 ГБ свободно | 20 ГБ |
| ОС | Debian 12 или Ubuntu 22.04/24.04, 64 бит | |

Нужен доступ `root` (или `sudo`) и открытые порты 80 и 443. Если на сервере уже работает nginx с
другими сайтами — это нормально: панель встанет рядом отдельным сайтом.

Панель без нагрузки с несколькими пользователями и обоими ботами занимает около 800 МБ RAM (семь
контейнеров), образы — около 700 МБ диска; при 1 ГБ RAM добавьте файл подкачки.

**Домен.** Подойдёт поддомен, например `panel.example.com`.

## 2. DNS

У регистратора домена создайте A-запись: имя — ваш поддомен, значение — IP сервера панели.
Проверьте, что запись уже видна (от нескольких минут до нескольких часов):

```bash
dig +short panel.example.com      # должен вывести IP вашего сервера
```

Пока DNS не обновился, сертификат HTTPS не выдаётся: установщик остановится с понятной ошибкой,
и его можно просто запустить ещё раз.

## 3. Установка одной командой

На сервере от `root`:

```bash
curl -fsSL https://raw.githubusercontent.com/AGLamake/Lamnezia/releases/install.sh | bash -s -- --domain panel.example.com
```

Скрипт:

1. ставит `git`, `curl`, `openssl` и Docker, если их нет;
2. скачивает последний релиз панели (ветка `releases`; другой тег или ветку задаёт `LAMNEZIA_REF`,
   например `LAMNEZIA_REF=dev`) в `/opt/lamnezia`;
3. создаёт `/opt/lamnezia/.env` со случайными секретами и свободным локальным портом;
4. собирает и запускает контейнеры, ждёт, пока панель ответит;
5. настраивает HTTPS:
   - если на сервере есть **nginx** — добавляет сайт `/etc/nginx/sites-available/lamnezia` и получает
     сертификат Let's Encrypt через `certbot` (он продлевается сам);
   - если nginx нет — запускает встроенный **Caddy**, он сам получает и продлевает сертификат.

Параметры: `--email you@example.com` — адрес для писем Let's Encrypt об истечении сертификата,
`--proxy nginx|caddy` — выбрать прокси вручную, `--dir` — другой каталог. Справка: `bash install.sh --help`.

### Готовый образ вместо сборки

`--image` берёт собранный образ из GHCR вместо сборки на сервере — быстрее и не нужна память на
сборку; образы есть для `amd64` и `arm64`:

```bash
curl -fsSL https://raw.githubusercontent.com/AGLamake/Lamnezia/releases/install.sh | bash -s -- --domain panel.example.com --image 1.2.0
```

Значение — версия (`1.2.0`, `1.2`), `latest` или полное имя образа. Скрипт записывает его в `.env`
как `LAMNEZIA_IMAGE=ghcr.io/aglamake/lamnezia:1.2.0`, и дальше запускает контейнеры командами
`docker compose pull && docker compose up -d --no-build`. Чтобы вернуться к сборке, удалите строку
`LAMNEZIA_IMAGE` из `.env`. Для версии образа стоит взять тот же тег кода: `LAMNEZIA_REF=v1.2.0`.

## 4. Первый администратор

```bash
cd /opt/lamnezia
docker compose exec web python manage.py createadmin --username admin
```

Задайте пароль, откройте `https://panel.example.com/` и войдите. Администратор сначала подключает
приложение-аутентификатор (двухфакторный вход, см. [Безопасность](Security.md#двухфакторный-вход)).

## 5. Установка вручную

То же самое без скрипта.

**Docker:**

```bash
curl -fsSL https://get.docker.com | sh
```

**Код и настройки:**

```bash
git clone https://github.com/AGLamake/Lamnezia.git /opt/lamnezia
cd /opt/lamnezia
cp .env.example .env
chmod 600 .env
```

В `.env` задайте `LAMNEZIA_DOMAIN` и сгенерируйте секреты:

```bash
openssl rand -hex 32                          # LAMNEZIA_SECRET_KEY
openssl rand -hex 24                          # LAMNEZIA_DB_PASSWORD
openssl rand -base64 32 | tr '+/' '-_'        # LAMNEZIA_ENCRYPTION_KEY
```

Все параметры описаны в [Настройка](Configuration.md).

**Запуск:**

```bash
docker compose up -d --build
docker compose ps                 # web, worker, poller, postgres, redis — healthy
```

Миграции базы применяются сами при каждом старте `web`.

**HTTPS через nginx сервера.** Панель слушает только `LAMNEZIA_HTTP_BIND` (по умолчанию
`127.0.0.1:8000`); если порт занят, поменяйте его в `.env` и выполните `docker compose up -d`.

```bash
apt install -y nginx certbot python3-certbot-nginx
cp deploy/nginx/lamnezia.conf /etc/nginx/sites-available/lamnezia
nano /etc/nginx/sites-available/lamnezia   # домен и порт из LAMNEZIA_HTTP_BIND
ln -s /etc/nginx/sites-available/lamnezia /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d panel.example.com --redirect
```

**HTTPS через встроенный Caddy** (если nginx на сервере нет и порты 80/443 свободны):
добавьте в `.env` строку `COMPOSE_PROFILES=caddy` и выполните `docker compose up -d`.

## 6. Проверка

- `https://panel.example.com/healthz` отвечает `{"status": "ok"}` — панель работает и видит базу.
- `docker compose ps` — у `web`, `worker`, `poller`, `postgres`, `redis` статус `healthy`.
- `wss://panel.example.com/ws/ping` отвечает `pong` на любое сообщение — WebSocket проходит через прокси.

## 7. Что дальше

- [Настройка](Configuration.md): все параметры; [Безопасность](Security.md): что проверить.
- [Серверы и импорт](Servers-and-import.md): добавьте первый VPN-сервер.
- [Бэкапы](Backup.md), [Обновление](Upgrade.md), [Решение проблем](Troubleshooting.md).

## Сервисы

| Сервис | Что делает |
|---|---|
| `web` | Django (ASGI: gunicorn + uvicorn), HTTP и WebSocket |
| `worker` | Фоновые задачи Celery: SSH-операции, уведомления; опрос — когда свободен |
| `poller` | Только опрос серверов: мониторинг и трафик. Не ждёт за долгими установками; потоков — `LAMNEZIA_POLL_WORKERS` (2) |
| `beat` | Расписание фоновых задач |
| `postgres` | База данных (том `postgres-data`) |
| `redis` | Брокер задач и канал WebSocket |
| `caddy` | Только с профилем `caddy`: HTTPS без nginx |

Состояние хранится в томах `postgres-data`, `media` (и `caddy-data` с профилем `caddy`).
