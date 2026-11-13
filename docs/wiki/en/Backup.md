# Backup

Three things need saving:

- **the database**: servers, keys, users, settings;
- **the `.env` file**: it holds `LAMNEZIA_ENCRYPTION_KEY`. Without it the stored SSH credentials and
  client keys cannot be decrypted, even with the database;
- **the `media` volume**: uploaded files (support screenshots).

The panel keeps two kinds of backups:

- **panel backups**: the database and the `media` volume, saved by the `backup` service of
  `compose.yaml` into `/opt/lamnezia/backups`; `.env` is saved by you;
- **server keys**: the database holds the keys of every AmneziaWG 2.0 server and every client, and
  every hour the panel also copies the files of each VPN protocol from the server: `awg0.conf` and
  `clientsTable` of AmneziaWG 2.0, the folders of the other protocols, the keys of the Tor site,
  the Telegram proxies and SFTP. With them a protocol or a service is installed on a new VPS with
  the same keys.

## Panel backups

The `backup` service starts with the panel. Right away and then once a day it writes two files:

- `db-YYYYMMDD-HHMMSS.dump`: the database (`pg_dump -Fc`);
- `media-YYYYMMDD-HHMMSS.tar.gz`: the `media` volume.

The time in the names is UTC. The 14 newest files of each kind are kept, older ones are deleted.
A file appears under its name only when it is complete; if a backup fails, the previous ones stay
and the error is in `docker compose logs backup`. The files are readable by root only.

Settings in `.env`:

| Variable | Default | What it sets |
|---|---|---|
| `LAMNEZIA_BACKUP_INTERVAL` | `86400` | Seconds between backups. |
| `LAMNEZIA_BACKUP_KEEP` | `14` | Backups of each kind to keep. |
| `LAMNEZIA_BACKUP_DIR` | `./backups` | Host directory for the backups. |

After changing them run `docker compose up -d backup`. One backup right now:

```bash
docker compose run --rm backup sh /usr/local/bin/backup.sh once
```

**Save `.env` separately**, once and after every change: it holds `LAMNEZIA_ENCRYPTION_KEY`. The
backups do not contain it on purpose. Without it the SSH credentials and keys in the database
cannot be decrypted.

Backups on the same machine do not survive the loss of the machine. Copy the directory elsewhere,
for example by cron on another server:

```bash
rsync -a root@panel.example.com:/opt/lamnezia/backups/ /srv/lamnezia-backups/
```

## Restoring the panel

Restore on a server where the panel has not been started yet: the PostgreSQL password is set when the
database starts for the first time and must match the one in the saved `.env`.

```bash
git clone https://github.com/AGLamake/Lamnezia.git /opt/lamnezia
cd /opt/lamnezia
cp /root/lamnezia.env .env && chmod 600 .env
docker compose up -d --wait postgres
docker compose exec -T postgres pg_restore -U lamnezia -d lamnezia --no-owner < /root/backups/db-20260101-030000.dump
docker compose run --rm --no-deps --user root -v /root/backups:/backups web \
    tar -xzf /backups/media-20260101-030000.tar.gz -C /app
docker compose up -d --build
```

Here `/root/lamnezia.env` is the saved `.env` and `/root/backups` holds the copied backups; take the
newest pair of files. VPN servers keep working while the panel is down. If a VPN server was lost
too, restore it after the panel: [Restoring a protocol](#restoring-a-protocol-on-a-new-vps).

Then set up HTTPS: run `install.sh` (it keeps the restored `.env`) or follow [Installation, section 5](Installation.md#5-manual-install).
If the domain changed, update `LAMNEZIA_DOMAIN` in `.env`; if the `LAMNEZIA_HTTP_BIND` port is taken
on the new server, pick another one.

## Copies of the server files

Every `LAMNEZIA_PROTOCOL_BACKUP_INTERVAL` seconds (an hour by default) the panel reads `awg0.conf`
and `clientsTable` of every AmneziaWG 2.0 server and keeps them encrypted in the database. The
copy is only read, nothing on the server changes. A server that does not answer keeps its last
copy. A server where AmneziaWG now runs with another server key does not replace the copy either:
its files are not the ones the panel keys belong to.

Of the other VPN protocols the panel copies the folders where the container keeps its state, as
they are, in a `tar.gz` archive:

| Protocol | Folders | What they hold |
|---|---|---|
| AmneziaWG legacy (`amnezia-awg`) | `/opt/amnezia/awg` | `wg0.conf`, the preshared key, `clientsTable` |
| WireGuard (`amnezia-wireguard`) | `/opt/amnezia/wireguard` | `wg0.conf`, the preshared key, `clientsTable` |
| OpenVPN (`amnezia-openvpn`) | `/opt/amnezia/openvpn` | the easy-rsa CA, `index.txt`, the revocation list, `server.conf` |
| XRay (`amnezia-xray`) | `/opt/amnezia/xray` | `server.json`, the Reality keys, `clientsTable` |
| IPsec (`amnezia-ipsec`) | `/etc/ipsec.d`, `/opt/amnezia/ikev2` | the NSS database with the CA and certificates |

The copy is taken only when the files belong to the server key the panel has: the private key of
WireGuard and XRay, the CA of OpenVPN and IPsec.

Of the services the panel copies what their users know them by:

| Service | Folders | What they hold |
|---|---|---|
| Tor site (`amnezia-torwebsite`) | `/var/lib/tor/hidden_service`, `/var/www/html/wp-content`, `wp-config.php`, `.htaccess` | the keys of the onion address, the WordPress site and a dump of its database |
| MTProxy (`amnezia-mtproxy`) | `/data` | the secret of the links and the install settings |
| Telemt (`amnezia-telemt`) | `/data` | the secret, `config.toml` with the settings and switches |
| SFTP (`amnezia-sftp`) | `/etc/ssh`, `/home` | the SSH host keys and the uploaded files |

A service is copied only while its container runs with what the panel has: the same onion
address, proxy secret or SFTP login and password. The copy of the Tor site and of SFTP may be up
to 256 MB unpacked (64 MB for the others); a larger one is not taken and the last copy stays. The
WordPress core is not copied: the image brings it. SOCKS5 has nothing of its own to keep (its
login and password are in the panel) and the DNS service has no state.

**A new proxy secret is in the copy at once.** **New secret** or new settings of a Telegram proxy
ask for a copy of the service right after the operation: the old copy holds the old secret, which
the panel no longer knows the proxy by.

**Keys issued just now are in the copy too.** Besides the hourly copy, a key issued, disabled,
enabled or revoked asks for one more copy of its protocol about 30 seconds later; the key
operations of those 30 seconds share it. If the server does not answer at that moment, the last
copy stays and the next hourly copy catches up.

**Deleting a server does not delete its copies.** A server whose protocols or services have
copies cannot be deleted in one go: the panel refuses. Remove its protocols and services first
(ticking **Delete all backups?** for a protocol), then delete the server.

The **Backup** section of the server page shows when the last copy was taken. A protocol other
than AmneziaWG 2.0, or a service, shows **Restore on this server** only when it has a copy.

Removing the container on the server page keeps the copy and the keys unless **Delete all
backups?** is ticked ([Protocols](Protocols.md#container-restart-stop-log-removal)). Right before
the container is removed the panel takes a fresh copy of its files; if that fails, the last copy
stays, and the operation log says so. The protocol is then marked **container removed**, is no
longer read, and **Restore on this server** brings it back with the same clients.

The removal dialog warns when a protocol other than AmneziaWG 2.0 has no copy yet: if the copy
before the removal fails too, its keys cannot work again, even after a new install.

## Restoring a protocol on a new VPS

When a VPN server is lost, its protocol can be installed on a new VPS with the old keys: the old
server private key, preshared key, port and AWG parameters, and every client keeps its address.

1. Create a VPS with Debian or Ubuntu, as for a new server ([Servers](Servers-and-import.md)).
2. If the address changes, point the DNS name of the server to the new VPS. **Clients keep working
   without changes only if the address in their configs stays the same**: the same IP, or the same
   DNS name when the server was added by name. Otherwise the server works, but clients need new
   keys from the portal.
3. Open the server page and click **Edit**: set the new address and login. The fingerprint of the
   old address is forgotten by itself. A new VPS with the old address has a new SSH host key:
   press **Forget** next to the SSH fingerprint on the server page, otherwise the panel refuses to
   connect.
4. In the **Backup** section click **Restore on this server**. The operation prepares the server,
   builds the container and writes the files; the progress is on the server page.
5. Check: the server check passes, and a client connects with the old config.

What the restore writes:

- the last copy of the server files, brought up to date with the panel keys: active keys issued
  after the copy are added, disabled ones are taken out of `awg0.conf` (their name stays in
  `clientsTable`), revoked ones are removed; clients added in the Amnezia app and kept in the copy
  stay;
- without a copy: the server settings from the panel and the active keys with their addresses;
- an active key without an address is skipped and named in the operation log.

The restore can be run again: a container that already has the same server key is kept. A server
where AmneziaWG 2.0 runs with another key is never overwritten; remove the `amnezia-awg2` container
there or restore to another VPS.

### Other protocols

AmneziaWG legacy, WireGuard, OpenVPN, XRay and IPsec are restored from the copy of their folders.
The operation builds the container as a new install does, puts the saved files in before the
container is configured, and the install scripts of the app then keep them: no new server keys,
CA or Reality keys are made, so the old client configs work again. Without a copy these protocols
are not restored.

- The copy is checked first: it must hold the server key the panel has for the protocol.
- A container already on the server is checked **before it is started**: one with the panel keys
  is kept, one with other keys is left as it is (not started, configured or overwritten) and the
  operation stops. Remove that container or restore to another VPS.
- Keys issued after the last copy are not in it (the copy is taken about 30 seconds after a key
  change, so this happens only when the server was lost right then): their number is in the
  operation log, reissue them.
- The IPsec server certificate names the server IP. On a VPS with another IP, reissue it after
  the restore: **Reissue** in the **IPsec server certificate** row of the server card
  ([Servers](Servers-and-import.md#new-address-of-a-server)); the backup section reminds of it.

### Services

The Tor site, MTProxy, Telemt and SFTP are restored from their copy with **Restore on this
server** in the **Backup** section: the service gets back the same onion address, the same proxy
links, the same SSH host keys of SFTP (clients do not see a *host key changed* warning). The copy
must hold what the panel has for the service; a container already on the server with other keys
is not started and the operation stops. Tor and sshd make keys of their own when the container
starts: the restore replaces them with the copy and restarts the container. The Tor site comes
back with its pages, uploads and database; SFTP with the files of its account.

A restore that failed half way (the connection dropped while the files were put in) leaves a
container with keys of its own. The panel marks such a container, and **Restore** run again
removes it and installs the service anew instead of refusing it. A container the panel did not
leave this way is still never overwritten.

## Full recovery

When both the panel and a VPN server are lost:

1. Restore the panel from its backup and the saved `.env`
   ([Restoring the panel](#restoring-the-panel)).
2. For every lost VPN server follow [Restoring a protocol](#restoring-a-protocol-on-a-new-vps).
3. Open **Keys → Differences with servers → Check now**: if the panel and the servers still differ,
   the differences are listed there ([Keys](Keys.md#differences-with-servers)).
