# Configuration

The panel reads its settings from environment variables with the `LAMNEZIA_` prefix, usually from
the `.env` file (see `.env.example`). Security-related settings are explained in
[Security](Security.md).

With docker compose, `LAMNEZIA_ALLOWED_HOSTS`, `LAMNEZIA_DATABASE_URL` and `LAMNEZIA_REDIS_URL`
are derived from the domain and the database password. After editing `.env`: `docker compose up -d`.

| Variable | Default | Meaning |
|---|---|---|
| `LAMNEZIA_SECRET_KEY` | — (required) | Django secret key: a long random string. |
| `LAMNEZIA_DEBUG` | `False` | Debug mode, only for local development. |
| `LAMNEZIA_ALLOWED_HOSTS` | empty | Comma-separated host names the panel answers on. |
| `LAMNEZIA_DATABASE_URL` | SQLite `db.sqlite3` | Database URL, e.g. `postgres://user:pass@host:5432/lamnezia`. |
| `LAMNEZIA_TIME_ZONE` | `UTC` | Time zone for dates in the interface. |
| `LAMNEZIA_DOMAIN` | — (required for compose) | Panel domain. |
| `LAMNEZIA_DB_PASSWORD` | — (required for compose) | PostgreSQL password, letters and digits only. |
| `LAMNEZIA_WEB_WORKERS` | `2` | Number of web processes. |
| `LAMNEZIA_POLL_WORKERS` | `2` | How many servers the `poller` service polls at once (monitoring and traffic). |
| `LAMNEZIA_HTTP_BIND` | `127.0.0.1:8000` | Address where web is published for the host's nginx. |
| `LAMNEZIA_TRUSTED_PROXIES` | loopback and private networks | Addresses or networks, separated by commas, whose `X-Forwarded-For` and `X-Forwarded-Proto` the panel reads; from others the headers are dropped ([Security](Security.md)). |
| `LAMNEZIA_IMAGE` | `lamnezia:latest` (local build) | Ready image, e.g. `ghcr.io/aglamake/lamnezia:1.2.0` ([installation](Installation.md#ready-image-instead-of-a-build)). |
| `LAMNEZIA_REDIS_URL` | empty | Redis for WebSocket, background jobs and the cache. |
| `LAMNEZIA_ENCRYPTION_KEY` | — (required) | Key that encrypts SSH credentials and private keys in the database. While the key is being changed, several keys separated by commas; the first one encrypts ([Changing the encryption key](Security.md#changing-the-encryption-key)). |
| `LAMNEZIA_SSH_CONNECT_TIMEOUT` | `15` | Seconds to connect and log in to a server. |
| `LAMNEZIA_SSH_COMMAND_TIMEOUT` | `120` | Seconds one command on a server may run. |
| `LAMNEZIA_SSH_LOCK_TIMEOUT` | `300` | Seconds to wait while another operation changes the same server. |
| `LAMNEZIA_TRAFFIC_INTERVAL` | `300` | Seconds between traffic readings from the servers; limits are applied as often ([Users, plans and limits](Users-plans-limits.md#applying-limits)). |
| `LAMNEZIA_RECONCILE_INTERVAL` | `900` | Seconds between comparisons of the panel keys with the servers. |
| `LAMNEZIA_PROTOCOL_BACKUP_INTERVAL` | `3600` | Seconds between copies of the server files (`awg0.conf`, `clientsTable`) into the panel ([Backup](Backup.md#copies-of-the-server-files)). |
| `LAMNEZIA_BACKUP_INTERVAL` | `86400` | Seconds between backups of the database and uploaded files. |
| `LAMNEZIA_BACKUP_KEEP` | `14` | Backups of each kind to keep. |
| `LAMNEZIA_BACKUP_DIR` | `./backups` | Host directory for the backups. |
| `LAMNEZIA_MONITOR_INTERVAL` | `60` | Seconds between server health readings. A server that does not answer for two intervals is shown offline. |
| `LAMNEZIA_METRICS_TOKEN` | empty | Token for the Prometheus endpoint `/metrics`. Empty keeps it off. |
| `LAMNEZIA_OPERATION_TIMEOUT` | `3600` | Seconds after which a waiting or running operation counts as interrupted. |
| `LAMNEZIA_MONITOR_HISTORY_DAYS` | `7` | Days of server health readings kept. |
| `LAMNEZIA_MONITOR_DETAIL_DAYS` | `1` | Days of readings kept in full; older whole days keep one reading per quarter of an hour. |
| `LAMNEZIA_OPERATION_RETENTION_DAYS` | `90` | Days a finished operation stays in the journal. |
| `LAMNEZIA_NOTIFICATION_RETENTION_DAYS` | `90` | Days a sent or failed notification delivery stays in the log. |
| `LAMNEZIA_TRAFFIC_HOURLY_DAYS` | `35` | Days of hourly traffic; older hours are folded into one row per key and day (at least 1). Totals, statistics and limits stay the same. |
| `LAMNEZIA_GITHUB_TOKEN` | empty | GitHub token without scopes. Raises the API limit for Amnezia release links. |
| `LAMNEZIA_AMNEZIA_APP_STORE_URL` | Amnezia in App Store | iOS download link in the guides. |
| `LAMNEZIA_AMNEZIA_GOOGLE_PLAY_URL` | Amnezia in Google Play | Google Play link in the guides. |
| `LAMNEZIA_REQUIRE_ADMIN_2FA` | `True` | Administrators must sign in with a code from an authenticator app. |
| `LAMNEZIA_LOGIN_FAILURE_LIMIT` | `5` | Wrong passwords in a row before signing in to an account from one address is paused. |
| `LAMNEZIA_LOGIN_LOCKOUT_MINUTES` | `15` | How long the pause lasts. |
| `LAMNEZIA_REGISTRATION_RATE_LIMIT` | `5` | Open registrations an hour from one address. `0` turns the limit off. |
| `LAMNEZIA_MAGIC_LINK_MINUTES` | `15` | How long a sign-in link from a letter works. |
| `LAMNEZIA_SUBSCRIPTION_RATE_LIMIT` | `30` | Subscription link fetches a minute from one address; more get `429`. `0` turns the limit off. |
| `LAMNEZIA_SUPPORT_RATE_LIMIT` | `5` | New support requests an hour from one user. `0` turns the limit off. |
| `LAMNEZIA_SUPPORT_MESSAGE_RATE_LIMIT` | `10` | Messages a minute from one user in the chat and requests. `0` turns the limit off. |
| `LAMNEZIA_SUGGESTION_RATE_LIMIT` | `3` | Article suggestions a day from one user ([Guides](Guides-and-releases.md#suggestions-from-users)). `0` turns the limit off. |
| `LAMNEZIA_SUPPORT_INCIDENT_WINDOW_MINUTES` | `30` | Reports that a server does not work, coming no further apart than this, are grouped into one incident. |
| `LAMNEZIA_SITE_URL` | `https://<LAMNEZIA_DOMAIN>` | Panel address for links the bots send. |
| `LAMNEZIA_BOT_RATE_LIMIT` | `20` | Commands a minute the bots take from one messenger account. `0` turns the limit off. |
| `LAMNEZIA_TELEGRAM_WEBHOOK` | `false` | The Telegram bot takes updates by a webhook at `<LAMNEZIA_SITE_URL>/bots/telegram/` instead of long polling. Needs a public HTTPS domain ([Telegram and Discord accounts](Messengers.md)). |
| `LAMNEZIA_EMAIL_URL` | `consolemail://` | Mail server for letters (password reset, email confirmation, sign-in links), e.g. `smtp+tls://user:password@smtp.example.com:587`. The default only writes letters to the `web` log. |
| `LAMNEZIA_DEFAULT_FROM_EMAIL` | `Lamnezia <noreply@localhost>` | Sender of the letters. |
| `LAMNEZIA_PLUGINS` | empty | Comma-separated Django apps added to the panel, e.g. payment plugins ([Billing plugins](Billing-plugins.md)). |
| `LAMNEZIA_HSTS_SECONDS` | `31536000` (`0` with `DEBUG`) | HSTS lifetime: browsers open the panel only over HTTPS. `0` turns HSTS off. |

## Panel settings

Settings that change without a restart live in **Settings** (`/settings/`, administrators only).
They are kept in the database (the `SiteSettings` model, one row).

| Field | Default | Meaning |
|---|---|---|
| Panel name | `Lamnezia` | Name shown in the menu and page titles. |
| Registration | By invitation only | By invitation only, open with automatic approval, or open with approval by an administrator ([Registration modes](Registration-modes.md)). |
| Default plan | none | Plan given to new users of open registration with automatic approval. |
| Confirm email on registration | on | Open registration sends a confirmation link when mail is set up. |
| Showing a key again | Any time | Any time, or only once: after the first view the key can only be re-issued ([Keys](Keys.md#showing-a-key)). |
| Keys in the bots | The key itself | The key itself, or only a link to the portal: Telegram and Discord chats are not end-to-end encrypted ([Messengers](Messengers.md#the-bots)). |
| Users suggest articles | On | Signed-in users send links and texts for the guides; off hides the form ([Guides](Guides-and-releases.md#suggestions-from-users)). |
| SMTP server, port, encryption | empty, `587`, STARTTLS | Mail server for letters; encryption is STARTTLS, SSL/TLS or none. While no server is set, the panel sends mail through `LAMNEZIA_EMAIL_URL`. |
| SMTP user, SMTP password | empty | Login on the mail server. |
| Sender | empty | `From` of the letters; empty uses `LAMNEZIA_DEFAULT_FROM_EMAIL`. |
| Telegram bot token | empty | Bot used by the Telegram [notification](Notifications.md) channels that have no token of their own, and for linking Telegram accounts. |
| Telegram bot username | empty | Without `@`; with the token, users can [link Telegram](Messengers.md). |
| Sign in with Telegram | off | Users with a linked Telegram sign in with it; needs `/setdomain` in @BotFather ([Messengers](Messengers.md)). |
| Discord application ID, Discord client secret | empty | Discord OAuth2 application for [linking Discord](Messengers.md). |
| Discord bot token | empty | The [Discord bot](Messengers.md#the-discord-bot) and Discord direct message notifications. |
| Sign in with Discord | off | Users with a linked Discord sign in with Discord. |
| GitHub token | empty | Overrides `LAMNEZIA_GITHUB_TOKEN` for Amnezia release links. |

Passwords and tokens are stored encrypted and are never shown again: leave the field empty to keep
the saved value, or tick **Remove the saved value**.

## Interface language

The interface is available in Russian and English. The RU/EN switcher sits next to the theme toggle.
Until a user picks a language, the panel follows the browser settings, falling back to Russian.
The choice is kept in the `django_language` cookie; for a signed-in user it also becomes the
language of their notifications (see [Notifications](Notifications.md)).
