# Installation

The panel runs on a VPS with docker compose. The Amnezia VPN servers it manages may be other machines:
the panel reaches them over SSH. Installing the panel on the VPN server itself also works.

## 1. Requirements

**VPS for the panel**

| | Minimum | Recommended |
|---|---|---|
| CPU | 1 core | 2 cores |
| RAM | 1 GB | 2 GB |
| Disk | 10 GB free | 20 GB |
| OS | Debian 12 or Ubuntu 22.04/24.04, 64-bit | |

You need `root` (or `sudo`) access and open ports 80 and 443. If nginx already serves other sites on
the server, that is fine: the panel becomes one more site.

An idle panel with a few users and both bots takes about 800 MB of RAM (seven containers) and the
images about 700 MB of disk; with 1 GB of RAM add a swap file.

**A domain.** A subdomain such as `panel.example.com` is enough.

## 2. DNS

At your domain registrar create an A record: the name is your subdomain, the value is the IP of the
panel server. Check that it is visible (it takes from minutes to a few hours):

```bash
dig +short panel.example.com      # must print your server IP
```

Until DNS is updated no HTTPS certificate can be issued: the installer stops with a clear error and
can simply be run again.

## 3. One-command install

On the server, as `root`:

```bash
curl -fsSL https://raw.githubusercontent.com/AGLamake/Lamnezia/releases/install.sh | bash -s -- --domain panel.example.com
```

The script:

1. installs `git`, `curl`, `openssl` and Docker when missing;
2. downloads the latest release of the panel (the `releases` branch; another tag or branch with
   `LAMNEZIA_REF`, for example `LAMNEZIA_REF=dev`) to `/opt/lamnezia`;
3. creates `/opt/lamnezia/.env` with random secrets and a free local port;
4. builds and starts the containers and waits for the panel to answer;
5. sets up HTTPS:
   - if **nginx** is installed, it adds the site `/etc/nginx/sites-available/lamnezia` and gets a
     Let's Encrypt certificate with `certbot` (renewed automatically);
   - without nginx it starts the bundled **Caddy**, which gets and renews the certificate itself.

Options: `--email you@example.com` for Let's Encrypt expiry notices, `--proxy nginx|caddy` to pick the
proxy, `--dir` for another directory. Help: `bash install.sh --help`.

### Ready image instead of a build

`--image` takes the built image from GHCR instead of building on the server: faster, and no memory
is needed for the build; images exist for `amd64` and `arm64`:

```bash
curl -fsSL https://raw.githubusercontent.com/AGLamake/Lamnezia/releases/install.sh | bash -s -- --domain panel.example.com --image 1.2.0
```

The value is a version (`1.2.0`, `1.2`), `latest` or a full image reference. The script writes it to
`.env` as `LAMNEZIA_IMAGE=ghcr.io/aglamake/lamnezia:1.2.0` and from then on starts the containers with
`docker compose pull && docker compose up -d --no-build`. To build again, delete the
`LAMNEZIA_IMAGE` line from `.env`. Take the same code tag as the image: `LAMNEZIA_REF=v1.2.0`.

## 4. First administrator

```bash
cd /opt/lamnezia
docker compose exec web python manage.py createadmin --username admin
```

Enter a password, open `https://panel.example.com/` and sign in. The administrator first connects an
authenticator app (two-factor login, see [Security](Security.md#two-factor-login)).

## 5. Manual install

The same steps without the script.

**Docker:**

```bash
curl -fsSL https://get.docker.com | sh
```

**Code and settings:**

```bash
git clone https://github.com/AGLamake/Lamnezia.git /opt/lamnezia
cd /opt/lamnezia
cp .env.example .env
chmod 600 .env
```

In `.env` set `LAMNEZIA_DOMAIN` and generate the secrets:

```bash
openssl rand -hex 32                          # LAMNEZIA_SECRET_KEY
openssl rand -hex 24                          # LAMNEZIA_DB_PASSWORD
openssl rand -base64 32 | tr '+/' '-_'        # LAMNEZIA_ENCRYPTION_KEY
```

All settings are described in [Configuration](Configuration.md).

**Start:**

```bash
docker compose up -d --build
docker compose ps                 # web, worker, poller, postgres, redis are healthy
```

Database migrations run on every `web` start.

**HTTPS with the host's nginx.** The panel listens on `LAMNEZIA_HTTP_BIND` only (`127.0.0.1:8000` by
default); if the port is taken, change it in `.env` and run `docker compose up -d`.

```bash
apt install -y nginx certbot python3-certbot-nginx
cp deploy/nginx/lamnezia.conf /etc/nginx/sites-available/lamnezia
nano /etc/nginx/sites-available/lamnezia   # domain and the port from LAMNEZIA_HTTP_BIND
ln -s /etc/nginx/sites-available/lamnezia /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d panel.example.com --redirect
```

**HTTPS with the bundled Caddy** (when there is no nginx and ports 80/443 are free): add the line
`COMPOSE_PROFILES=caddy` to `.env` and run `docker compose up -d`.

## 6. Checks

- `https://panel.example.com/healthz` returns `{"status": "ok"}`: the panel runs and reaches the database.
- `docker compose ps` shows `healthy` for `web`, `worker`, `poller`, `postgres` and `redis`.
- `wss://panel.example.com/ws/ping` answers `pong` to any message: WebSocket passes through the proxy.

## 7. Next steps

- [Configuration](Configuration.md): all settings; [Security](Security.md): what to check.
- [Servers and import](Servers-and-import.md): add the first VPN server.
- [Backup](Backup.md), [Upgrade](Upgrade.md), [Troubleshooting](Troubleshooting.md).

## Services

| Service | Role |
|---|---|
| `web` | Django (ASGI: gunicorn + uvicorn), HTTP and WebSocket |
| `worker` | Celery background jobs: SSH operations, notifications; polling when it is free |
| `poller` | Server polling only: monitoring and traffic. It never waits behind long installs; `LAMNEZIA_POLL_WORKERS` (2) sets its threads |
| `beat` | Background job schedule |
| `postgres` | Database (`postgres-data` volume) |
| `redis` | Job broker and WebSocket channel layer |
| `caddy` | Only with the `caddy` profile: HTTPS without nginx |

State lives in the `postgres-data` and `media` volumes (plus `caddy-data` with the `caddy` profile).
