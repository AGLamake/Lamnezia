<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="static/brand/logo-dark.svg">
    <img src="static/brand/logo-light.svg" alt="Lamnezia" width="320">
  </picture>
</p>

# Lamnezia

[![CI](https://github.com/AGLamake/Lamnezia/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/AGLamake/Lamnezia/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/github/license/AGLamake/Lamnezia)](LICENSE)
[![Release](https://img.shields.io/github/v/release/AGLamake/Lamnezia?include_prereleases)](https://github.com/AGLamake/Lamnezia/releases)

**English** · [Русский](README_RU.md)

Lamnezia is a self-hosted web panel for Amnezia VPN servers. It imports servers set up in the Amnezia
app, issues keys to users, applies plans and traffic limits, shows statistics and gives users a
portal with their keys, guides and support. The panel keeps the server files in the format of the
Amnezia app, so the app keeps working with the same servers.

It is for people who run Amnezia VPN for others: friends and family, a team, a small community or
a paid service. Instead of passing `vpn://` links by hand, users sign in, get keys within their plan
and see their traffic; the administrator sees every server and user in one place.

> **Lamnezia is an independent project and is not affiliated with the Amnezia VPN team.**
> Amnezia is a trademark of its owners; the panel only uses the open file formats of the app.

## Contents

- [Features](#features)
- [Screenshots](#screenshots)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Examples](#examples)
- [Example `.env`](#example-env)
- [Documentation](#documentation)
- [How to help](#how-to-help)
- [License](#license)

## Features

- **Servers**: add a VPS over SSH, check it, import AmneziaWG 2.0 with its keys from a server set up
  in the Amnezia app, monitor CPU, memory, disk and containers.
- **Protocols**: prepare a clean server and install AmneziaWG 2.0 from the interface, change the
  protocol settings, restart, stop or remove the container, read its log.
- **Keys**: issue, show as a `vpn://` link, QR code or `.conf` file, disable and revoke; clients
  added in the Amnezia app are picked up, differences with the server are shown.
- **Users and plans**: plans with traffic per period, total traffic, key count, access term and
  servers; personal limits; keys turn off at a limit and back on when the user is within it again;
  bulk actions.
- **Registration**: by invitation, open, or open with approval; email confirmation and sign-in by
  email link.
- **Traffic and statistics**: per key, user and server, charts, top consumers, CSV export and a
  Prometheus endpoint.
- **Client portal**: keys, limits, server status, install guides with links to Amnezia releases,
  support requests and a live chat.
- **Support and incidents**: inbox for administrators, "server does not work" reports grouped into
  incidents.
- **Telegram and Discord**: users link their accounts and sign in with them; the bots give keys,
  status and guides, open support requests and bring the staff's answers.
- **Notifications**: email, Telegram, Discord and signed webhooks, with per-channel event routing.
- **Security and operations**: roles, two-factor login for administrators, encrypted SSH secrets,
  audit log, every server change as a background operation with a log, backups of the panel and of
  the server files, releases with ready images and automatic updates from GitHub.
- **Interface** in Russian and English, light and dark theme.

## Screenshots

| Dashboard | Statistics |
|---|---|
| [![Dashboard](docs/images/dashboard-en.png)](docs/images/dashboard-en.png) | [![Statistics](docs/images/stats-en.png)](docs/images/stats-en.png) |
| **Users** | **Client portal** |
| [![Users](docs/images/users-en.png)](docs/images/users-en.png) | [![Client portal](docs/images/portal-en.png)](docs/images/portal-en.png) |

The screenshots show demo data.

## Requirements

**VPS for the panel**

| | Minimum | Recommended |
|---|---|---|
| CPU | 1 core | 2 cores |
| RAM | 1 GB | 2 GB |
| Disk | 10 GB free | 20 GB |
| OS | Debian 12 or Ubuntu 22.04/24.04, 64-bit | |
| Software | Docker with the compose plugin (the install script sets it up) | |

Ports 80 and 443 must be open. An nginx that already serves other sites is fine: the panel becomes
one more site. On the test server an idle panel with a few users and both bots takes about 800 MB
of RAM (all seven containers) and under 1% CPU; the images take about 700 MB of disk, the database
grows with the traffic history. With 1 GB of RAM add a swap file.

**VPN servers**: the same as for the Amnezia app — a VPS with Debian or Ubuntu, SSH access as `root`
or a user with passwordless `sudo`, Linux kernel 4.14 or newer, at least 1 GB of free disk. The
panel can run on the VPN server itself or on a separate machine.

**A domain**: a subdomain such as `panel.example.com` with an A record pointing to the panel VPS.

## Quick start

On the panel VPS, as `root`:

```bash
dig +short panel.example.com    # must print the IP of this server
curl -fsSL https://raw.githubusercontent.com/AGLamake/Lamnezia/main/install.sh | bash -s -- --domain panel.example.com --email you@example.com
cd /opt/lamnezia
docker compose exec web python manage.py createadmin --username admin
```

The script installs Docker, generates the secrets in `/opt/lamnezia/.env`, starts the panel and sets
up HTTPS (through the nginx and certbot already on the server, or the bundled Caddy). Open
`https://panel.example.com`, sign in as `admin` and connect an authenticator app: two-factor login
is required for administrators. Then add your first server in **Servers**.

Manual installation, HTTPS options and checks: [Installation](docs/wiki/en/Installation.md).

## Examples

### VPN for friends

1. Set up a VPN server in the Amnezia app (AmneziaWG 2.0), as usual.
2. In the panel: **Servers → Add a server** with the same SSH access, then **Import from the
   server**. Existing clients appear as keys and keep working; assign them to their owners.
3. **Plans**: create a plan "Friends" without a traffic limit, keys at most 3.
4. **Settings → Registration**: keep "By invitation only"; in **Registration** create an invite
   with the plan "Friends".
5. Send the invite link. Friends register, press **Get a key** in the portal and open it in the
   Amnezia app; the guides in the portal show how to install the app.

### Selling access with plans

1. Create plans, e.g. "Trial" (5 GB, 7 days, trial), "Monthly 100 GB" (100 GB a period of 30 days,
   access 30 days, price 300). The price is for reference: payments are accepted outside the panel.
2. **Settings → Registration**: "Open, approved automatically" with "Trial" as the default plan, or
   "Open, approved by an administrator" to check each new user.
3. After a payment extend the access: **Users** → tick the users → **Extend access** for 30 days, or
   assign another plan. When the term or traffic runs out, the keys turn off by themselves and come
   back after an extension.
4. In **Notifications** subscribe a channel (e.g. Telegram) to "Limit reached", "Access ends soon"
   and "Registration request", and watch **Statistics**.

### Connecting an existing Amnezia server

The panel works with a server the Amnezia app has already set up and does not break it for the app:

1. **Servers → Add a server**: address, SSH user and key or password, the same as in the app.
2. The panel checks the server (connection, root access, OS, Docker, disk); nothing is changed.
3. **Import from the server** only reads `awg0.conf` and `clientsTable` of the AmneziaWG 2.0
   container and adds its clients as keys without an owner. Assign them to users or leave them as
   they are.
4. Keys issued by the panel appear in the Amnezia app, and clients added in the app appear in the
   panel. See [Servers and import](docs/wiki/en/Servers-and-import.md).

## Example `.env`

The install script writes `/opt/lamnezia/.env` itself. The required part looks like this; every
variable is in [`.env.example`](.env.example) and [Configuration](docs/wiki/en/Configuration.md).

```dotenv
# Domain of the panel; its DNS A record points to this server.
LAMNEZIA_DOMAIN=panel.example.com
# Django secret key: openssl rand -base64 48
LAMNEZIA_SECRET_KEY=change-me
# PostgreSQL password, letters and digits only.
LAMNEZIA_DB_PASSWORD=change-me
# Encrypts SSH credentials and private keys. Keep it with the backups.
LAMNEZIA_ENCRYPTION_KEY=

LAMNEZIA_TIME_ZONE=Europe/Moscow
LAMNEZIA_HTTP_BIND=127.0.0.1:8000
# Letters: password reset, email confirmation, sign-in links.
LAMNEZIA_EMAIL_URL=smtp+tls://user:password@smtp.example.com:587
LAMNEZIA_DEFAULT_FROM_EMAIL=Lamnezia <noreply@panel.example.com>
```

## Documentation

The [wiki](docs/wiki/en/Home.md) (also published to the GitHub Wiki):

- administration: [Installation](docs/wiki/en/Installation.md),
  [Configuration](docs/wiki/en/Configuration.md), [Upgrade](docs/wiki/en/Upgrade.md),
  [Backup](docs/wiki/en/Backup.md), [Security](docs/wiki/en/Security.md),
  [Troubleshooting](docs/wiki/en/Troubleshooting.md);
- features: [Servers and import](docs/wiki/en/Servers-and-import.md),
  [Protocols](docs/wiki/en/Protocols.md), [Keys](docs/wiki/en/Keys.md),
  [Users, plans and limits](docs/wiki/en/Users-plans-limits.md),
  [Registration](docs/wiki/en/Registration-modes.md),
  [Traffic and stats](docs/wiki/en/Traffic-and-stats.md),
  [Support](docs/wiki/en/Support-and-incidents.md), [Notifications](docs/wiki/en/Notifications.md),
  [Webhooks](docs/wiki/en/Webhooks.md), [Guides and releases](docs/wiki/en/Guides-and-releases.md),
  [Client portal](docs/wiki/en/Portal.md), [Telegram and Discord](docs/wiki/en/Messengers.md),
  [Setting up the bots](docs/wiki/en/Bots.md), [Audit log](docs/wiki/en/Audit.md);
- development: [Architecture](docs/wiki/en/Architecture.md),
  [Development](docs/wiki/en/Development.md), [Code style](docs/wiki/en/Code-style.md).

Changes by version: [CHANGELOG.md](CHANGELOG.md).

## How to help

- Try the panel and [open an issue](https://github.com/AGLamake/Lamnezia/issues) with a bug or an
  idea; security issues go by [SECURITY.md](SECURITY.md), not to public issues.
- Improve the translations (`locale/`) and the [wiki](docs/wiki/en/Home.md).
- Send a pull request: start with [CONTRIBUTING.md](CONTRIBUTING.md) and
  [Development](docs/wiki/en/Development.md) (local setup and checks).
- Star the repository and tell people who run Amnezia VPN.

## License

[AGPL-3.0](LICENSE). If you run a changed version of the panel as a service for others, you must
offer them its source code. See also [NOTICE](NOTICE).

Developed with the help of Claude (Anthropic) as a coding assistant.
