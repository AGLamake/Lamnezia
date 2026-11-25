# Security

How the panel protects access to itself, to the servers and to the keys, and what the administrator
has to take care of. Vulnerabilities are reported privately: see
[SECURITY.md](../../../SECURITY.md).

## Roles

Administrator, server administrator, moderator and user. What each of them sees and may do —
the levels of the sections, the zones, invites and the bot commands — is on the
[Roles and access](Roles-and-access.md) page.

The first administrator is created with a command; it asks for the password:

```bash
docker compose exec web python manage.py createadmin --username admin --email admin@example.com
```

For scripts, pass the password on standard input: `echo "$PASSWORD" | ... createadmin --username admin --password-stdin`.

## Two-factor login

With `LAMNEZIA_REQUIRE_ADMIN_2FA=True` (the default) an administrator or a server administrator
connects an authenticator app (Google Authenticator, Aegis, 2FAS and others) on the first sign-in
and then enters a code from it after the password; until then no page of the panel opens. Moderators and users can turn it on in
the **Two-factor** section of the **Profile** (the user name in the menu).

### Backup codes

When the app is connected, the panel issues 10 backup codes and shows them once: save them in a
password manager or print them. Without the phone, enter any backup code instead of the code from
the app (case and dashes do not matter); each code works once. **Security → Backup codes** shows
how many codes are left and, after a code from the app, issues a new set; all the previous codes
stop working. Connecting another phone issues a new set too.

In the audit log: `accounts.backup_codes_issue` — a set of codes was issued,
`accounts.backup_code_use` — a sign-in with a backup code (with the number left). The codes
themselves are never logged.

If both the phone and the codes are lost:

```bash
docker compose exec web python manage.py reset_2fa admin
```

The command turns off both the app and the backup codes.

## Brute force and rate limits

After `LAMNEZIA_LOGIN_FAILURE_LIMIT` wrong passwords for one account from one address, signing in
for that pair is paused for `LAMNEZIA_LOGIN_LOCKOUT_MINUTES`. Lockouts are listed in the Django admin
site `/admin/` → **Access attempts**; deleting a record lifts it.

Open registration, support requests and chat messages have their own limits
(`LAMNEZIA_REGISTRATION_RATE_LIMIT`, `LAMNEZIA_SUPPORT_RATE_LIMIT`,
`LAMNEZIA_SUPPORT_MESSAGE_RATE_LIMIT`, see [Configuration](Configuration.md)).

The client address is taken from the last `X-Forwarded-For` entry, which the nginx or Caddy in
front of the panel adds; the entries before it come from the client and are ignored, so they do not
change the lockouts, the limits or the address in the audit log.

`X-Forwarded-For` and `X-Forwarded-Proto` are read only from the proxies listed in
`LAMNEZIA_TRUSTED_PROXIES`: by default loopback and the private networks (`10.0.0.0/8`,
`172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`), where the host's nginx (through the docker gateway)
and the bundled Caddy come from. From any other address the panel drops both headers, so a forged
address or `https` is ignored. If the proxy is on another machine, list its address or network,
e.g. `LAMNEZIA_TRUSTED_PROXIES=192.0.2.10`. Still **do not publish the `web` container directly to
the internet**: keep `LAMNEZIA_HTTP_BIND` on `127.0.0.1`.

**Password reset** and sign-in links are sent by email, so set up mail first (**Settings** or
`LAMNEZIA_EMAIL_URL`).

### Sign-in alerts

A sign-in from a network or a browser the account has not signed in from before is reported to
its owner where they get notifications (`sign_in_new_place`): with the browser, the address and a
link to the **Sessions** of the profile to end a session they do not know and change the password.
A network is the /24 of an IPv4 address or the /64 of an IPv6 one, so a new address from the same
provider is not news; the very first sign-in of an account is not reported. The panel keeps only a
keyed hash of the network and the browser name to compare with, not the address, and forgets a
place not seen for 180 days. Administrators and moderators always get these alerts; a portal user
turns them on in **Profile → Notifications**.

A lockout after wrong passwords is reported to the channels subscribed to `sign_in_locked`
([Notifications](Notifications.md)), once per lockout, with the name typed in and the address.
Neither the password nor a 2FA code ever gets into a notification or the audit log.

## Encryption of secrets

SSH passwords and keys, client private keys, copies of the server files, SMTP passwords, bot tokens
and webhook secrets are stored in the database encrypted with `LAMNEZIA_ENCRYPTION_KEY`. Forms never
show a saved secret again, and secrets are never written to logs or to the audit log.

Generate the key once:

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store it together with your database backups, but not inside them ([Backup](Backup.md)). Without
the key the saved SSH credentials and client keys cannot be decrypted, and servers have to be added
again. Keep `.env` readable by root only (`chmod 600 .env`).

The panel checks the key when it starts (`manage.py migrate` and `manage.py check` in the `web`
container) and stops with a clear error instead of failing at the first secret:

- `lamnezia.E001`: `LAMNEZIA_ENCRYPTION_KEY` is empty;
- `lamnezia.E002`: the value is not a Fernet key;
- `lamnezia.E003`: the key is valid but does not decrypt the secrets already in the database, for
  example after a typo or a removed old key during a change.

### Changing the encryption key

Change the key when `.env` may have leaked or when someone who knew it leaves. The setting accepts
several keys separated by commas: the first one encrypts, all of them decrypt.

1. Generate a new key and put it **in front of** the old one:
   `LAMNEZIA_ENCRYPTION_KEY=<new key>,<old key>`.
2. Apply it: `docker compose up -d`.
3. Re-encrypt the stored secrets with the new key:

   ```bash
   docker compose exec web python manage.py rotate_encryption_key
   ```

   The command prints how many records it rewrote; running it again is safe. It also rewrites the
   secrets kept in the parameters of operations (the SOCKS5 password), so an operation that failed
   before the change can still be retried.
4. Remove the old key from the setting (`LAMNEZIA_ENCRYPTION_KEY=<new key>`), run
   `docker compose up -d` again and save the new `.env` outside the server.

Database backups made before the change are still encrypted with the old key: keep it next to them
while you keep those backups.

## Servers

- The panel connects over SSH as `root` or a user with passwordless sudo. An SSH key is safer than a
  password.
- Give the panel **its own SSH key** instead of the one you log in with: then you can revoke the
  panel's access without losing yours, and a leak of one key does not open the other. On your
  computer:

  ```bash
  ssh-keygen -t ed25519 -f lamnezia_key -C lamnezia -N ""
  ssh-copy-id -i lamnezia_key.pub root@vpn.example.com
  ```

  Paste the contents of `lamnezia_key` into the server form, then delete the file. To accept the key
  only from the panel host, start its line in `~/.ssh/authorized_keys` on the VPN server with
  `from="<panel IP>" `.
- On the first connection the panel remembers the server host key. If the key changes later, the
  connection is refused: the server was reinstalled or the connection is intercepted. After a
  deliberate reinstall delete the old record in `/admin/` → **Ssh → Known hosts**.
- SSH commands run only in background operations, never during a web request; every operation is in
  **Operations** with its log.

### Cloud metadata service

A cloud server answers on `169.254.169.254` with its own data and the tokens of its service account
(AWS, GCP, Azure, Yandex Cloud and others). A VPN client goes out to the network through the
server, so it could ask that service too. The panel closes the whole link-local range
`169.254.0.0/16` and the IPv6 metadata address `fd00:ec2::254` (AWS) to the Amnezia containers:

- the rule drops traffic that comes from the Docker bridges of the containers: the default
  `bridge` (`docker0`) and `amnezia-dns-net`. It covers every protocol and service, AmneziaWG and
  XRay included; the containers and their configs do not change;
- with Docker on iptables (the default) the rule is in the `DOCKER-USER` chain of the server (its
  own chain `LAMNEZIA-METADATA`), and for IPv6 the same in `ip6tables` if Docker keeps it for
  containers with IPv6;
- with Docker on its native nftables backend (`firewall-backend: nftables`, Docker 28+) there is
  no `DOCKER-USER`, and the rule is in a table of its own, `inet lamnezia_metadata`, with a chain
  on the forward hook, as the Docker documentation advises;
- the server itself still reaches the metadata service: its own traffic does not pass these
  chains;
- a systemd timer `lamnezia-metadata.timer` puts the rule back within a minute after a reboot, a
  Docker restart or a firewall reload. The script and the units are in `/opt/lamnezia/metadata/`
  and `/etc/systemd/system/`.

An install closes the server on its own after the container is set up. An import changes nothing
on the server itself and starts a separate operation, **Close the cloud metadata service after an
import or a panel update**. The panel starts the same operation every hour for servers added
before the block or closed by an older version of its script; each such server is tried once per
version of the script. A server without systemd, or one that did not answer, is left open, and
the operation log says why; the server card closes it.

The server card has **Cloud metadata service for VPN clients**: the state, *Check*, *Close* and
*Open*. Open it only if VPN clients need a local service of the hoster in `169.254.0.0/16` (for
example a DNS resolver of the cloud on `169.254.169.253`). The choice is kept: later installs and
imports and the hourly check leave such a server open until it is closed on the card again.

## Firewall of the panel host

The panel needs only HTTP and HTTPS from the internet; PostgreSQL, Redis and the `web` container
listen inside docker or on `127.0.0.1`. With `ufw`:

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp          # better: ufw allow from <your IP> to any port 22 proto tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
```

Docker publishes ports past `ufw`, so check with `ss -tlnp` that only nginx or Caddy and SSH listen
on a public address. The panel connects to the VPN servers itself (outgoing SSH), so the servers
need no rules for the panel apart from SSH.

## Privacy of VPN users

The panel never records which sites or addresses VPN users visit. It keeps only traffic volumes per
key and the time of the latest handshake. The [reachability check](Portal.md#reachability-check)
runs in the user's browser and its result never reaches the panel; the probe container on the server
keeps no logs.

The services the panel installs keep no visit logs either:

- **AmneziaDNS** (Unbound) logs no queries or replies of clients: the panel writes `verbosity: 0`
  and turns `log-queries`, `log-replies`, `log-servfail` and the other query logs off in
  `forward-records.conf`. The setting applies when the container is built. A DNS container built by
  the app is marked on the server card, and *Rebuild* builds it again with the logs off.
- **SOCKS5** (3proxy) is installed without the `log` line of the app script. A proxy set up in the
  app keeps `/usr/local/3proxy/logs/3proxy.log` with every host users visited. The import, and an
  install over such a proxy, notes it in the operation log, and the server card shows its size and
  *Delete the log*. The operation writes 3proxy.cfg the way the panel does, with the same user name
  and password, restarts the proxy and deletes the file; the app keeps connecting as before. If
  3proxy.cfg has no account (`auth none`) or the container is stopped, the log is left as it is.
- **XRay** installed by the panel has the access log off (`"access": "none"`).

## Web

Responses carry a Content Security Policy, HSTS over HTTPS (`LAMNEZIA_HSTS_SECONDS`), and cookies
that are `Secure` and `HttpOnly`. The Prometheus endpoint `/metrics` is off until
`LAMNEZIA_METRICS_TOKEN` is set and then requires the token ([Traffic and stats](Traffic-and-stats.md#prometheus)).

The CSP allows scripts only from the panel itself (`script-src 'self'`): no `'unsafe-eval'`, no
inline scripts and no `on…=` handlers. Dangerous actions (revoking a key, deleting a server and so
on) ask for confirmation in a panel dialog. Errors 400, 403, 404, 500 and a CSRF failure have their
own pages without technical details; the CSRF reason goes to the log only.

## Checklist

- [ ] `LAMNEZIA_DEBUG=False`, strong `LAMNEZIA_SECRET_KEY` and `LAMNEZIA_DB_PASSWORD`.
- [ ] `.env` is `chmod 600` and saved outside the server, with `LAMNEZIA_ENCRYPTION_KEY`.
- [ ] `LAMNEZIA_HTTP_BIND` listens on `127.0.0.1`; only ports 80/443 (and SSH) are open on the panel host.
- [ ] Two-factor login is on for administrators.
- [ ] Servers are added with a separate SSH key of the panel; their host keys are not reset
  without a reason.
- [ ] The firewall of the panel host lets in only 80/443 and SSH.
- [ ] Backups are copied to another machine ([Backup](Backup.md)).
- [ ] The panel is updated when a release fixes security issues ([Upgrade](Upgrade.md)).
