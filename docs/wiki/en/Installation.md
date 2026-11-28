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

**A domain** (optional). A subdomain such as `panel.example.com` is enough. Without a domain the
panel opens by the server's IP address, see [Without a domain](#without-a-domain).

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
   `LAMNEZIA_REF`, for example `LAMNEZIA_REF=dev`) to `/opt/lamnezia`. A branch stays a local branch
   tracking `origin`, so `git pull` updates the code later; a tag installs that version and stays
   on it;
3. creates `/opt/lamnezia/.env` with random secrets and a free local port;
4. builds and starts the containers and waits for the panel to answer;
5. sets up HTTPS:
   - if **nginx** is installed, it adds the site `/etc/nginx/sites-available/lamnezia` and gets a
     Let's Encrypt certificate with `certbot` (renewed automatically);
   - without nginx it starts the bundled **Caddy**, which gets and renews the certificate itself.

Options: `--email you@example.com` for Let's Encrypt expiry notices, `--proxy nginx|caddy` to pick the
proxy, `--dir` for another directory. Help: `bash install.sh --help`.

### Without systemd

The installer adds systemd timers and units: the certificate retry during a
[panel move](Panel-move.md), the stop of the old copy after a move and updates from the **Update**
page. A host without systemd (a container, WSL, some VPS images) gets the panel without them: the
install goes on, and at the end it prints what does not work and how to do it by hand:

| Does not work | By hand |
|---|---|
| Certificate retry while the domain points elsewhere | once the domain points here: `certbot --nginx -d panel.example.com --redirect` |
| Stop of this copy after a panel move | once the domain points to the new machine: `cd /opt/lamnezia && docker compose down` |
| Updates from the **Update** page | `cd /opt/lamnezia && git pull && docker compose up -d --build`, see [Upgrade](Upgrade.md) |
| Docker start after a reboot, if no init script starts it | `update-rc.d docker defaults` or start `dockerd` when the host boots; the panel containers come back by themselves |
| Let's Encrypt renewal with nginx, if cron does not run | start cron (`apt-get install cron`) or `certbot renew` once a month |

nginx is reloaded with `nginx -s reload` instead of `systemctl`. The Docker and cron rows show
only when the installer finds no start link of Docker in `/etc/rc2.d`–`/etc/rc5.d` or no running
`cron`.

With systemd the installer checks that Docker starts at boot and warns otherwise:
`systemctl enable docker`.

### Without a domain

Without `--domain` (or with an empty answer to the question) the panel opens by the server's public
IPv4 address: `https://203.0.113.5/`. The script finds the address from outside, writes it to
`LAMNEZIA_DOMAIN` and sets `LAMNEZIA_CADDY_TLS=ip`; the bundled Caddy gets a Let's Encrypt
certificate for the IP address. Such a certificate lives about 6 days, and Caddy renews it by
itself. The certificate is issued over port 80, so ports 80 and 443 must be open and free.

```bash
curl -fsSL https://raw.githubusercontent.com/AGLamake/Lamnezia/releases/install.sh | bash
```

- Only the bundled Caddy works this way: certbot gives no certificate for an IP address. If nginx
  is installed, the script stops with an error. Pass `--proxy caddy` if nginx does not hold
  ports 80/443, or use a domain.
- Links in letters, notifications and bot messages lead to `https://<IP>/`.
- Keep the Telegram bot on long polling (the default). The webhook by an IP address has not been
  checked. Sign-in with Telegram needs a domain linked to the bot in BotFather (`/setdomain`).
- A [panel move](Panel-move.md) of such a panel installs the copy at the IP address of the new
  machine. There is no A record to switch: open the panel at the new address.

**Switching to a domain later.** Point the domain's A record to the server, set
`LAMNEZIA_DOMAIN=panel.example.com` in `/opt/lamnezia/.env` (and `LAMNEZIA_SITE_URL`, if you set it)
and run the install command again. It keeps `.env`, removes `LAMNEZIA_CADDY_TLS`, and Caddy gets an
ordinary certificate for the domain. Messengers that use webhooks or sign-in links need the new
address too.

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
How to check that the image is genuine: [Upgrade](Upgrade.md#checking-that-a-release-is-genuine).

`--restore FILE` installs the panel from a [panel archive](Backup.md#from-a-panel-archive): with its
database, secrets and files instead of new ones.

### Without questions (cloud-init)

Without a terminal — cloud-init, a provider's startup script, `ssh host 'bash install.sh'` — or with
`LAMNEZIA_UNATTENDED=1` the script asks nothing. The options may then come from variables:

| Variable | Option | Meaning |
|---|---|---|
| `LAMNEZIA_DOMAIN` | `--domain` | panel domain; empty: the server's IP address |
| `LAMNEZIA_EMAIL` | `--email` | address for Let's Encrypt notices |
| `LAMNEZIA_PROXY` | `--proxy` | `auto`, `nginx` or `caddy` |
| `LAMNEZIA_IMAGE` | `--image` | ready image: a version, `latest` or a full reference |
| `LAMNEZIA_DIR` | `--dir` | installation directory |
| `LAMNEZIA_REF` | — | branch or tag of the code, `releases` by default |
| `LAMNEZIA_ADMIN_USERNAME` | — | name of the first administrator, `admin` by default |

An option given on the command line wins over its variable. A new installation without questions
also creates the first administrator with a generated password, so step 4 is not needed. The panel
address, the administrator and the password are written to `/opt/lamnezia/install-summary.txt`,
readable by root only; the password does not appear in the output. This password is temporary:
at the first sign-in the panel opens only the page to choose a new one. Then set up two-factor
login and delete the file, it still holds the old password; until then **Needs attention** on the
administrator's dashboard reminds of it. Running the script again does not touch the file or
the accounts. With a terminal everything works as before, the file only tells the address and the
next step.

An example of user data for cloud-init is in
[`deploy/cloud-init/user-data.yaml`](https://github.com/AGLamake/Lamnezia/blob/releases/deploy/cloud-init/user-data.yaml):
set the domain and the address, paste it into the provider's "user data" field and create the
server. The DNS record must point to the server before it boots, otherwise the certificate is not
issued. The install log is in `/var/log/cloud-init-output.log`.

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

State lives in the `postgres-data`, `media` and `archives` volumes (plus `caddy-data` with the `caddy` profile).
