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

**Домен** (не обязательно). Подойдёт поддомен, например `panel.example.com`. Без домена панель
открывается по IP-адресу сервера, см. [Без домена](#без-домена).

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
   например `LAMNEZIA_REF=dev`) в `/opt/lamnezia`. Ветка остаётся локальной веткой, которая следит
   за `origin`, так что дальше код обновляет `git pull`; тег ставит эту версию и на ней остаётся;
3. создаёт `/opt/lamnezia/.env` со случайными секретами и свободным локальным портом;
4. собирает и запускает контейнеры, ждёт, пока панель ответит;
5. настраивает HTTPS:
   - если на сервере есть **nginx** — добавляет сайт `/etc/nginx/sites-available/lamnezia` и получает
     сертификат Let's Encrypt через `certbot` (он продлевается сам);
   - если nginx нет — запускает встроенный **Caddy**, он сам получает и продлевает сертификат.

Параметры: `--email you@example.com` — адрес для писем Let's Encrypt об истечении сертификата,
`--proxy nginx|caddy` — выбрать прокси вручную, `--dir` — другой каталог. Справка: `bash install.sh --help`.

### Без systemd

Установщик ставит таймеры и юниты systemd: повтор запроса сертификата при
[переезде панели](Panel-move.md), остановку старой копии после переезда и обновление со страницы
**Обновление**. На хосте без systemd (контейнер, WSL, некоторые образы VPS) панель ставится без них:
установка идёт дальше, а в конце печатает, что не работает и как сделать это вручную:

| Не работает | Вручную |
|---|---|
| Повтор запроса сертификата, пока домен ведёт на другой сервер | когда домен поведёт сюда: `certbot --nginx -d panel.example.com --redirect` |
| Остановка этой копии после переезда панели | когда домен поведёт на новую машину: `cd /opt/lamnezia && docker compose down` |
| Обновление со страницы **Обновление** | `cd /opt/lamnezia && git pull && docker compose up -d --build`, см. [Обновление](Upgrade.md) |
| Запуск Docker после перезагрузки, если его не запускает init-скрипт | `update-rc.d docker defaults` или запуск `dockerd` при старте хоста; контейнеры панели поднимутся сами |
| Продление сертификата Let's Encrypt с nginx, если cron не запущен | запустить cron (`apt-get install cron`) или `certbot renew` раз в месяц |

nginx перечитывает настройки через `nginx -s reload`, а не `systemctl`. Строки про Docker и cron
появляются, только если установщик не нашёл ссылку запуска Docker в `/etc/rc2.d`–`/etc/rc5.d` или
запущенный `cron`.

С systemd установщик проверяет, что Docker включён при загрузке, и иначе предупреждает:
`systemctl enable docker`.

### Без домена

Без `--domain` (или с пустым ответом на вопрос) панель открывается по внешнему IPv4-адресу сервера:
`https://203.0.113.5/`. Скрипт узнаёт адрес снаружи, записывает его в `LAMNEZIA_DOMAIN` и ставит
`LAMNEZIA_CADDY_TLS=ip`; встроенный Caddy получает сертификат Let's Encrypt на IP-адрес. Такой
сертификат живёт около 6 дней, и Caddy продлевает его сам. Сертификат выдаётся через порт 80,
поэтому порты 80 и 443 должны быть открыты и свободны.

```bash
curl -fsSL https://raw.githubusercontent.com/AGLamake/Lamnezia/releases/install.sh | bash
```

- Так работает только встроенный Caddy: certbot не выдаёт сертификат на IP-адрес. Если на сервере
  есть nginx, скрипт остановится с ошибкой. Передайте `--proxy caddy`, если nginx не занимает
  порты 80/443, или возьмите домен.
- Ссылки в письмах, уведомлениях и сообщениях ботов ведут на `https://<IP>/`.
- Telegram-бота оставьте на long polling (по умолчанию). Webhook по IP-адресу не проверялся. Вход
  через Telegram требует домена, привязанного к боту в BotFather (`/setdomain`).
- [Перенос панели](Panel-move.md) с таким адресом ставит копию на IP-адрес новой машины. A-запись
  переключать не нужно: откройте панель по новому адресу.

**Как потом перейти на домен.** Направьте A-запись домена на сервер, задайте
`LAMNEZIA_DOMAIN=panel.example.com` в `/opt/lamnezia/.env` (и `LAMNEZIA_SITE_URL`, если он задан) и
снова выполните команду установки. Она сохранит `.env`, уберёт `LAMNEZIA_CADDY_TLS`, и Caddy получит
обычный сертификат на домен. Мессенджерам с webhook или ссылками входа тоже нужен новый адрес.

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
Как проверить подлинность образа — [Обновление](Upgrade.md#проверка-подлинности-релиза).

`--restore ФАЙЛ` ставит панель из [архива панели](Backup.md#из-архива-панели): с её базой,
секретами и файлами вместо новых.

### Без вопросов (cloud-init)

Без терминала — в cloud-init, стартовом скрипте провайдера, `ssh host 'bash install.sh'` — или с
`LAMNEZIA_UNATTENDED=1` скрипт ничего не спрашивает. Параметры тогда можно передать переменными:

| Переменная | Параметр | Значение |
|---|---|---|
| `LAMNEZIA_DOMAIN` | `--domain` | домен панели; пусто — IP-адрес сервера |
| `LAMNEZIA_EMAIL` | `--email` | адрес для уведомлений Let's Encrypt |
| `LAMNEZIA_PROXY` | `--proxy` | `auto`, `nginx` или `caddy` |
| `LAMNEZIA_IMAGE` | `--image` | готовый образ: версия, `latest` или полное имя |
| `LAMNEZIA_DIR` | `--dir` | каталог установки |
| `LAMNEZIA_REF` | — | ветка или тег кода, по умолчанию `releases` |
| `LAMNEZIA_ADMIN_USERNAME` | — | имя первого администратора, по умолчанию `admin` |

Параметр в командной строке важнее переменной. Новая установка без вопросов сразу создаёт первого
администратора со сгенерированным паролем, так что шаг 4 не нужен. Адрес панели, администратор и
пароль записываются в `/opt/lamnezia/install-summary.txt`, который читает только root; в выводе
пароля нет. Этот пароль временный: при первом входе панель открывает только страницу, где нужно
задать новый. Потом подключите двухфакторный вход и удалите файл — в нём остался старый пароль. Повторный запуск
скрипта не трогает ни файл, ни учётные записи. С терминалом всё работает как раньше, файл только
сообщает адрес и следующий шаг.

Пример user data для cloud-init —
[`deploy/cloud-init/user-data.yaml`](https://github.com/AGLamake/Lamnezia/blob/releases/deploy/cloud-init/user-data.yaml):
укажите домен и адрес, вставьте в поле «user data» у провайдера и создайте сервер. DNS-запись
должна указывать на сервер до его загрузки, иначе сертификат не выпустится. Журнал установки —
в `/var/log/cloud-init-output.log`.

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

Состояние хранится в томах `postgres-data`, `media`, `archives` (и `caddy-data` с профилем `caddy`).
