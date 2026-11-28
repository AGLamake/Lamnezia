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
| `LAMNEZIA_DOMAIN` | — (required for compose) | Panel domain, or the server's IP address (bundled Caddy only, see [Installation](Installation.md#without-a-domain)). |
| `LAMNEZIA_CADDY_TLS` | `domain` | Certificate the bundled Caddy gets: `ip` — a short-lived Let's Encrypt certificate for an IP address in `LAMNEZIA_DOMAIN`. `install.sh` sets it on every run. |
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
| `LAMNEZIA_RECONCILE_INTERVAL` | `900` | Seconds between comparisons of the panel keys with the servers and of group members with the leading server. |
| `LAMNEZIA_GROUP_CATCH_UP_INTERVAL` | `300` | Seconds between checks of the lagging members of server groups and their catch-up ([Server groups](Server-groups.md)). |
| `LAMNEZIA_RELAY_SYNC_INTERVAL` | `60` | Seconds between checks of the relays; one whose groups, members or member addresses in DNS changed is updated. A change made in the panel updates the relay at once ([Relay](Server-groups.md#relay)). |
| `LAMNEZIA_RELAY_MONITOR_INTERVAL` | `60` | Seconds between readings of the relay state: the active member, who the relay reaches and the channel load. A relay without a reading for two intervals is reported (`relay_down`). |
| `LAMNEZIA_GROUP_LAG_ALERT` | `3600` | Seconds a group member may lag behind its group before a notification (`member_lagging`). |
| `LAMNEZIA_PROTOCOL_BACKUP_INTERVAL` | `3600` | Seconds between copies of the server files (`awg0.conf`, `clientsTable`) into the panel ([Backup](Backup.md#copies-of-the-server-files)). |
| `LAMNEZIA_BACKUP_INTERVAL` | `86400` | Seconds between backups of the database and uploaded files. |
| `LAMNEZIA_BACKUP_KEEP` | `14` | Backups of each kind to keep. |
| `LAMNEZIA_PANEL_ARCHIVE_KEEP` | `3` | Newest panel archives kept in the `archives` volume ([Panel archive](Backup.md#panel-archive)). |
| `LAMNEZIA_PANEL_ARCHIVE_UPLOAD_MB` | `20` behind nginx, `0` with Caddy | The largest archive the reverse proxy lets through the restore form, in MB; the page names it and shows the command for a larger one. `0` is no limit. Change it together with `client_max_body_size` of nginx. |
| `LAMNEZIA_PANEL_ARCHIVE_PASSWORD` | empty | Password of [scheduled archives](Backup.md#on-schedule), at least 12 characters; empty turns the schedule off. It is not put into the archive. |
| `LAMNEZIA_PANEL_ARCHIVE_INTERVAL` | `24` | Hours between scheduled archives. |
| `LAMNEZIA_PANEL_ARCHIVE_REMOTE` | empty | Where a copy of every archive goes: `s3://bucket/folder` or `sftp://user@host:22/folder` ([External storage](Backup.md#external-storage)). |
| `LAMNEZIA_PANEL_ARCHIVE_REMOTE_ENDPOINT` | `https://s3.amazonaws.com` | Address of the S3-compatible storage. |
| `LAMNEZIA_PANEL_ARCHIVE_REMOTE_REGION` | `us-east-1` | Region the S3 signature names. |
| `LAMNEZIA_PANEL_ARCHIVE_REMOTE_ACCESS_KEY`, `LAMNEZIA_PANEL_ARCHIVE_REMOTE_SECRET_KEY` | empty | S3 keys. |
| `LAMNEZIA_PANEL_ARCHIVE_REMOTE_PASSWORD` | empty | SFTP password. |
| `LAMNEZIA_PANEL_ARCHIVE_REMOTE_HOST_KEY` | empty | SFTP server key, a line of `ssh-keyscan HOST`. |
| `LAMNEZIA_UPDATE_CHECK` | `True` | Ask GitHub twice a day for a new release of the panel and offer the update ([Update from the panel](Upgrade.md#update-from-the-panel)). Only the release tag is fetched; nothing is sent. |
| `LAMNEZIA_BACKUP_DIR` | `./backups` | Host directory for the backups. |
| `LAMNEZIA_MONITOR_INTERVAL` | `60` | Seconds between server health readings. A server that does not answer for two intervals is shown offline. |
| `LAMNEZIA_STEALTH_CHECK_INTERVAL` | `86400` | Seconds between scheduled masking checks of a server; `0` turns them off. |
| `LAMNEZIA_METRICS_TOKEN` | empty | Token for the Prometheus endpoint `/metrics`. Empty keeps it off. |
| `LAMNEZIA_OPERATION_TIMEOUT` | `3600` | Seconds after which a waiting or running operation counts as interrupted. |
| `LAMNEZIA_MONITOR_HISTORY_DAYS` | `7` | Days of server health readings and relay load readings kept. |
| `LAMNEZIA_MONITOR_DETAIL_DAYS` | `1` | Days of readings kept in full; older whole days keep one reading per quarter of an hour. |
| `LAMNEZIA_OPERATION_RETENTION_DAYS` | `90` | Days a finished operation stays in the journal. |
| `LAMNEZIA_NOTIFICATION_RETENTION_DAYS` | `90` | Days a sent or failed notification delivery stays in the log. |
| `LAMNEZIA_API_CALL_RETENTION_DAYS` | `30` | Days an **API call** record stays in the audit log; older ones are deleted once a day, other events are not touched. |
| `LAMNEZIA_TRAFFIC_HOURLY_DAYS` | `35` | Days of hourly traffic; older hours are folded into one row per key and day (at least 1). Totals, statistics and limits stay the same. |
| `LAMNEZIA_GITHUB_TOKEN` | empty | GitHub token without scopes. Raises the API limit for Amnezia release links. |
| `LAMNEZIA_GITHUB_PROXY` | empty | A proxy for the requests to api.github.com (Amnezia release links, the panel update check), like `LAMNEZIA_TELEGRAM_PROXY`. |
| `LAMNEZIA_MATERIALS_PROXY` | empty | A proxy for the weekly check of the useful material links, like `LAMNEZIA_TELEGRAM_PROXY`, so a site blocked where the panel runs is not marked broken. |
| `LAMNEZIA_AMNEZIA_APP_STORE_URL` | Amnezia in App Store | iOS download link in the guides. |
| `LAMNEZIA_AMNEZIA_GOOGLE_PLAY_URL` | Amnezia in Google Play | Google Play link in the guides. |
| `LAMNEZIA_REQUIRE_ADMIN_2FA` | `True` | Administrators and server administrators must sign in with a code from an authenticator app. |
| `LAMNEZIA_LOGIN_FAILURE_LIMIT` | `5` | Wrong passwords in a row before signing in to an account from one address is paused. |
| `LAMNEZIA_LOGIN_LOCKOUT_MINUTES` | `15` | How long the pause lasts. |
| `LAMNEZIA_REGISTRATION_RATE_LIMIT` | `5` | Open registrations an hour from one address. `0` turns the limit off. |
| `LAMNEZIA_MAGIC_LINK_MINUTES` | `15` | How long a sign-in link from a letter works. |
| `LAMNEZIA_SUBSCRIPTION_RATE_LIMIT` | `30` | Subscription link fetches a minute from one address; more get `429`. `0` turns the limit off. |
| `LAMNEZIA_API_RATE_LIMIT` | `120` | [API](API.md) calls a minute with one token, and as many calls with a wrong token from one address; more get `429`. `0` turns the limit off. |
| `LAMNEZIA_SUPPORT_RATE_LIMIT` | `5` | New support requests an hour from one user. `0` turns the limit off. |
| `LAMNEZIA_SUPPORT_MESSAGE_RATE_LIMIT` | `10` | Messages a minute from one user in the chat and requests. `0` turns the limit off. |
| `LAMNEZIA_SUGGESTION_RATE_LIMIT` | `3` | Article suggestions a day from one user ([Guides](Guides-and-releases.md#suggestions-from-users)). `0` turns the limit off. |
| `LAMNEZIA_SUPPORT_INCIDENT_WINDOW_MINUTES` | `30` | Reports that a server does not work, coming no further apart than this, are grouped into one incident. |
| `LAMNEZIA_SITE_URL` | `https://<LAMNEZIA_DOMAIN>` | Panel address for links the bots send. |
| `LAMNEZIA_BOT_RATE_LIMIT` | `20` | Commands a minute the bots take from one messenger account. `0` turns the limit off. |
| `LAMNEZIA_TELEGRAM_WEBHOOK` | `false` | The Telegram bot takes updates by a webhook at `<LAMNEZIA_SITE_URL>/bots/telegram/` instead of long polling. Needs a public HTTPS domain ([Telegram and Discord accounts](Messengers.md)). |
| `LAMNEZIA_TELEGRAM_PROXY` | empty | A proxy for the bot and the Telegram notifications where Telegram is blocked: `http://host:port` or `socks5://host:port`, maybe with `user:password@` ([Setting up the bots](Bots.md#telegram-through-a-proxy)). The password is never logged and is hidden on the error page (`LAMNEZIA_DEBUG`). |
| `LAMNEZIA_TELEGRAM_API_SERVER` | `https://api.telegram.org` | A self-hosted Bot API server for the bot and the Telegram notifications, for example `http://telegram-bot-api:8081`. |
| `LAMNEZIA_DISCORD_PROXY` | empty | A proxy for the Discord bot, its direct messages and the notification webhooks on discord.com, like `LAMNEZIA_TELEGRAM_PROXY` ([Setting up the bots](Bots.md#discord-through-a-proxy)). |
| `LAMNEZIA_EMAIL_URL` | `consolemail://` | Mail server for letters (password reset, email confirmation, sign-in links), e.g. `smtp+tls://user:password@smtp.example.com:587`. The default only writes letters to the `web` log. |
| `LAMNEZIA_DEFAULT_FROM_EMAIL` | `Lamnezia <noreply@localhost>` | Sender of the letters. |
| `LAMNEZIA_PLUGINS` | empty | Comma-separated Django apps added to the panel, e.g. payment plugins ([Billing plugins](Billing-plugins.md)). |
| `LAMNEZIA_DOMAIN_RESOLVERS` | `system,1.1.1.1,8.8.8.8` | Resolvers the [panel move](Panel-move.md#move-progress-screen) asks where the panel domain points: `system` and IP addresses of public resolvers, comma separated. Empty: the system one only. |
| `LAMNEZIA_ORDER_EXPIRES_HOURS` | `24` | Hours an order waits for payment before it is marked expired ([Billing plugins](Billing-plugins.md)). |
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
