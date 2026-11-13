# Troubleshooting

Logs of all services: `docker compose logs -f` (one service: `docker compose logs -f web`).
Restart: `docker compose restart`. Everything the panel did on a server is in **Operations** with
the steps, the log and, for a failed operation, what to do.

## Installation and access

| Symptom | What to check |
|---|---|
| certbot: `Challenge failed`, `NXDOMAIN` | DNS does not point to the server yet (`dig +short <domain>`), or a firewall closes port 80 |
| `502 Bad Gateway` from nginx | The panel is down or the port differs: `docker compose ps`, `docker compose logs web`, `LAMNEZIA_HTTP_BIND` and `proxy_pass` in the nginx site |
| `Bad Request (400)` | The panel is opened by a name other than `LAMNEZIA_DOMAIN`: the domain in `.env` and in the address bar must match |
| CSRF error on login | The nginx site lacks `proxy_set_header X-Forwarded-Proto $scheme;` |
| Operation progress does not update, chat does not work | WebSocket is blocked: the nginx site needs the `Upgrade` and `Connection` headers (they are in `deploy/nginx/lamnezia.conf`) |
| `Set the LAMNEZIA_ENCRYPTION_KEY environment variable` | The encryption key is missing in `.env` ([Installation](Installation.md#5-manual-install)) |
| `port is already allocated` | Another service uses the `LAMNEZIA_HTTP_BIND` port: pick another one |
| `web` never becomes `healthy`, logs say `password authentication failed` | `LAMNEZIA_DB_PASSWORD` was changed after the first start; restore the previous password |

## FAQ for the administrator

**The administrator lost the phone with the authenticator.**
`docker compose exec web python manage.py reset_2fa <username>`, then sign in and connect the app
again ([Security](Security.md#two-factor-login)).

**Signing in says the account is locked.**
Too many wrong passwords from one address. Wait `LAMNEZIA_LOGIN_LOCKOUT_MINUTES` or delete the
record in `/admin/` → **Access attempts**.

**Letters do not arrive (password reset, confirmation, notifications).**
While no SMTP server is set in **Settings** and `LAMNEZIA_EMAIL_URL` is not set, letters only go to
the log: `docker compose logs web`. After setting up mail send a test from a channel in
**Notifications**.

**A server check fails with a host key error.**
The server was reinstalled, or the connection is intercepted. If the reinstall was yours, delete the
old record in `/admin/` → **Ssh → Known hosts** and check the server again.

**A server check says the login was rejected or sudo needs a password.**
Check the user, the key or password, and that the user is `root` or has `NOPASSWD` in sudoers
([Servers and import](Servers-and-import.md#adding-a-server)).

**An operation hangs in "waiting" or "running".**
Another operation holds the lock of the same server, or the `worker` stopped:
`docker compose ps worker`, `docker compose logs worker`. After `LAMNEZIA_OPERATION_TIMEOUT` it is
marked interrupted and can be retried with **Retry**.

**A server is shown offline although it works.**
The panel reads it every `LAMNEZIA_MONITOR_INTERVAL` seconds; the server page shows the last error.
Usually SSH is closed by a firewall or the SSH port changed.

**Keys created in the Amnezia app are not in the panel.**
The panel compares keys with the servers every `LAMNEZIA_RECONCILE_INTERVAL` seconds. Open
**Keys → Differences with servers → Check now** and **Add to the panel**
([Keys](Keys.md#differences-with-servers)).

**A user's keys turned off by themselves.**
A limit or the access term was reached: the key status is "disabled by limit". Extend the access or
raise the limit, and the keys come back on at the next check
([Users, plans and limits](Users-plans-limits.md#applying-limits)).

**A protocol change stops with a hint about `amnezia-awg2-previous`.**
An earlier port change was interrupted. Compare the containers in `docker ps -a` on the server and
remove the one that is not needed ([Protocols](Protocols.md#protocol-settings)).

**The container log is empty.**
Containers started by the Amnezia app have logging turned off. Tick **Keep the container log** in
the protocol settings ([Protocols](Protocols.md#container-restart-stop-log-removal)).

**Users see "server cannot be reached" although it works.**
The reachability probe port may be closed at the hosting provider, or the user's network really
blocks UDP ([Servers and import](Servers-and-import.md#reachability-probe)).

**Amnezia download buttons are missing in the guides.**
GitHub could not be reached and no release was received yet; set `LAMNEZIA_GITHUB_TOKEN` if the
API limit is hit ([Guides and releases](Guides-and-releases.md#amnezia-app-downloads)).

**Upload through the VPN is several times slower than download.**
The container interface glues TCP packets (TSO/GSO). Press **Check** in **Upload speed through the
VPN (TSO/GSO)** on the server page; if TSO or GSO is `on`, press **Turn on**
([Protocols](Protocols.md#tsogso-workaround)).

**The VPN server is lost.**
Restore the protocol on a new VPS with the same keys ([Backup](Backup.md#restoring-a-protocol-on-a-new-vps)).
