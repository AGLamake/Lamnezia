# Backup

Three things need saving:

- **the database**: servers, keys, users, settings;
- **the `.env` file**: it holds `LAMNEZIA_ENCRYPTION_KEY`. Without it the stored SSH credentials and
  client keys cannot be decrypted, even with the database;
- **the `media` volume**: uploaded files (support screenshots).

The panel keeps three kinds of backups:

- **the panel archive**: all three in one file encrypted with a password; it is made on the
  **System → Panel archive** page or by a command, and restored on a new server with
  `install.sh --restore`;
- **panel backups**: the database and the `media` volume, saved by the `backup` service of
  `compose.yaml` into `/opt/lamnezia/backups`; `.env` is saved by you;
- **server keys**: the database holds the keys of every AmneziaWG 2.0 server and every client, and
  every hour the panel also copies the files of each VPN protocol from the server: `awg0.conf` and
  `clientsTable` of AmneziaWG 2.0, the folders of the other protocols, the keys of the Tor site,
  the Telegram proxies and SFTP. With them a protocol or a service is installed on a new VPS with
  the same keys.

## Panel archive

The archive is one file `lamnezia-YYYYMMDD-HHMMSS.lamnezia` (UTC time) with everything a new panel
needs: the database, `.env` with the secrets encryption key `LAMNEZIA_ENCRYPTION_KEY`, the `media`
volume. It is encrypted with a password (AES-256, the key derived by PBKDF2-SHA256 with 600,000
iterations) and signed with HMAC-SHA256; without the password it cannot be read. The panel does
not keep the password: lose it and the archive cannot be restored.

**From the panel.** **System → Panel archive**: type the password twice (at least 12 characters)
and press **Make an archive**. A background operation makes it; the password reaches it encrypted,
and the operation erases it before saving the database, so the password never gets into the
archive. The finished archive shows in the list: **Download** it and keep it away from this server.
Downloads and removals go to the [audit log](Audit.md).

**By a command** on the panel server:

```bash
cd /opt/lamnezia
docker compose exec web python manage.py panel_archive create
docker compose cp web:/app/archives/lamnezia-20260101-030000.lamnezia /root/
```

The password is asked twice; scripts pass `--password-stdin`. To check an archive (password and
checksums): `docker compose exec web python manage.py panel_archive verify /app/archives/NAME`.

Archives live in the `archives` volume; the newest `LAMNEZIA_PANEL_ARCHIVE_KEEP` (3 by default)
stay, older ones are removed when a new one is made. Only the panel user can read the files.

### On schedule

Set the password in `.env` and restart the panel (`docker compose up -d`):

```bash
LAMNEZIA_PANEL_ARCHIVE_PASSWORD='a long password, 12 characters or more'
LAMNEZIA_PANEL_ARCHIVE_INTERVAL=24
```

Every hour the panel checks the newest archive in the folder, made by hand too: when it is
`LAMNEZIA_PANEL_ARCHIVE_INTERVAL` hours old (half an hour of slack), a new archive is made by the
same background operation; one runs at a time. **System → Panel archive** shows the schedule.

The password stays only in `.env`: it does not get into the archive, the database or the operation
log. The panel cannot keep it in the database encrypted: the database and the encryption key are
inside the archive themselves. Keep the password elsewhere too — without it the archive cannot be
restored. A panel restored from an archive or moved does not have it: set it again there.

### External storage

To send a copy of every archive, by hand or on schedule, set `LAMNEZIA_PANEL_ARCHIVE_REMOTE`:

- **S3-compatible** (AWS S3, MinIO, Backblaze B2, Yandex Object Storage and the like):
  `s3://bucket/folder` with `LAMNEZIA_PANEL_ARCHIVE_REMOTE_ENDPOINT` (for example
  `https://storage.yandexcloud.net`), `LAMNEZIA_PANEL_ARCHIVE_REMOTE_REGION`,
  `LAMNEZIA_PANEL_ARCHIVE_REMOTE_ACCESS_KEY` and `LAMNEZIA_PANEL_ARCHIVE_REMOTE_SECRET_KEY`. The key
  needs only the right to put objects. One file up to 5 GB.
- **SFTP**: `sftp://user@host:22/folder` (a folder from the user's home; `//srv/folder` for an
  absolute one) with `LAMNEZIA_PANEL_ARCHIVE_REMOTE_PASSWORD` and the server key
  `LAMNEZIA_PANEL_ARCHIVE_REMOTE_HOST_KEY` — a line of `ssh-keyscan -p 22 host`. A server with
  another key is refused. The copy is written as `NAME.part` and renamed when complete.

The copy is the same encrypted file. The panel does not remove old copies: set a lifetime rule in
the storage. The storage keys go into the archive with the rest of `.env`, so a restored panel
keeps sending copies.

If the copy fails, the archive stays on the panel, the operation shows the step and the storage
answer, **System → Panel archive** shows the error until the next archive succeeds, and the
**Panel archive failed** notification (`panel_archive_failed`) goes to the subscribed channels,
once a day. Wrong storage settings do the same after the archive is made. The panel update archive
is not sent.

What is inside (a `tar.gz` under the encryption):

| File | What it is |
|---|---|
| `database.dump` | The database, `pg_dump --format=custom`. |
| `env` | The panel `.env`: every `LAMNEZIA_*` and `COMPOSE_*` variable but the scheduled archive password. |
| `media/` | Uploaded files. |
| `migrations.txt` | Applied migrations: a panel older than the archive refuses it. |
| `manifest.json`, `SHA256SUMS` | Panel version, time, checksums. |

The file is format 2: the line `LAMNEZIA-PANEL-ARCHIVE 2`, a 32-byte password check, what
`openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha256` writes, and a 32-byte HMAC-SHA256 of
everything before it. So a wrong password and a damaged file give different errors, in the panel,
in `panel_archive verify` and in the installer, and a damaged file is caught before anything is
unpacked. The installer needs no Python for it: `deploy/archive/unpack.sh` checks the archive with
openssl and coreutils.

Archives of format 1, made by earlier panels, are still checked and restored. They have no check
and no signature: a wrong password and a damaged file give the same error there.

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

### From a panel archive

To move a running panel to another machine, use [Panel move](Panel-move.md): it does the steps
below by itself.

On a new server (Debian or Ubuntu) where the panel was never installed, copy the archive and run
the installer with `--restore`:

```bash
curl -fsSL https://raw.githubusercontent.com/AGLamake/Lamnezia/releases/install.sh -o install.sh
bash install.sh --restore /root/lamnezia-20260101-030000.lamnezia
```

The installer asks for the archive password (or takes it from `LAMNEZIA_ARCHIVE_PASSWORD`),
decrypts and checks the archive, writes `.env` from it, restores the database and the files, starts
the panel and sets up HTTPS as a normal install does. Sign in with the old accounts: the panel sees
the same servers, keys and SSH credentials.

- Another domain: `--domain new.example.com`; its DNS record must point to the new server.
- The reverse proxy is chosen anew (`--proxy`), as on install.
- If the `LAMNEZIA_HTTP_BIND` port of the archive is taken on the new server, a free one is used.
- An archive of a newer panel than the code being installed is refused: install the same version or
  newer (`LAMNEZIA_REF=...` or `--image`).
- The installer does not restore into a running panel: if `.env` exists or the database is not
  empty, it stops and says how to start over. Use [Into a running panel](#into-a-running-panel).

VPN servers keep working while the panel is down. If a VPN server was lost too, restore it after the
panel: [Restoring a protocol](#restoring-a-protocol-on-a-new-vps).

### Into a running panel

**System → Panel archive → Restore from an archive** replaces the data of an installed panel with
an archive: upload the file or pick one of the archives on the panel, enter its password and
confirm. Only an administrator can do it. The restore runs in the worker as an operation; its page
shows each step.

1. The password, the checksums and the version are checked. A wrong password, a damaged file or an
   archive of a newer panel stop the restore here, and nothing changes.
2. If other operations run or wait, the restore stops and nothing changes.
3. The current state is saved into a **safety archive** with the password of the restored one. It
   stays in the list of archives; old archives are not removed at this step.
4. The database is replaced in one transaction: if PostgreSQL refuses the dump, the old database
   stays.
5. The uploaded files are replaced, the migrations of this version are applied and the secrets are
   re-encrypted with the key of this panel. If this fails, the safety archive is put back and the
   operation says so.

While the database is replaced, other jobs of the panel fail with "The panel is being restored",
and changes from the web get 503; reading pages works.

What to expect afterwards:

- Everyone signs in again, with the accounts and passwords of the archive.
- The `.env` of this panel stays: a running panel cannot change its own environment. The secrets
  key of the archive is used only to re-encrypt the SSH credentials and other secrets. The step log
  names the panel settings of the archive's `.env` that differ from this one (names only, no
  values); copy the ones you need into `.env` and restart the panel. The domain, the port, the image
  and the keys always stay those of this installation.
- The operation history of the archive replaces the current one; the restore operation itself is
  kept.

An archive bigger than the upload limit of the reverse proxy (20 MB in the nginx config of the
installer) does not pass through the page. Copy it to the server and restore it with the command:

```bash
docker compose cp lamnezia-20260101-030000.lamnezia web:/tmp/panel.lamnezia
docker compose exec web python manage.py panel_archive restore /tmp/panel.lamnezia
```

The command asks for the password (or reads it with `--password-stdin`), checks the archive at
once and queues the restore; follow it on the **Operations** page.

### From backups by hand

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
newest pair of files.

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
| Tor site (`amnezia-torwebsite`) | `/var/lib/tor/hidden_service`, `/var/www/html` | the keys of the onion address, the WordPress site with its core and a dump of its database |
| MTProxy (`amnezia-mtproxy`) | `/data` | the secret of the links and the install settings |
| Telemt (`amnezia-telemt`) | `/data` | the secret, `config.toml` with the settings and switches |
| SFTP (`amnezia-sftp`) | `/etc/ssh`, `/home` | the SSH host keys and the uploaded files |

A service is copied only while its container runs with what the panel has: the same onion
address, proxy secret or SFTP login and password. The copy of the Tor site and of SFTP may be up
to 256 MB unpacked (64 MB for the others); a larger one is not taken, the last copy stays, and
the **Backup** section shows a warning with the size the files take. The WordPress core is copied
with the site, so a restore brings back the version the site ran, not the one of the image.
SOCKS5 has nothing of its own to keep (its login and password are in the panel) and the DNS
service has no state.

**Where the copies are kept.** A service copy is a file in the `media` volume of the panel
(`service-backups/`), encrypted under a key of its own; that key is kept encrypted in the
database. The file is encrypted in chunks of 64 KiB (AES-256-GCM, each chunk checked on its own),
so saving and restoring a copy of hundreds of megabytes needs no second copy of it in memory; a
damaged, cut or reordered file is refused with the reason, and the restore stops before it
touches the server. The panel archive and the backup service take the folder along. Before the
hourly copy the panel asks the server for the size and a checksum of the files: unchanged since
the last copy, they are not sent again, and the **Backup** section says when they were last
checked. After a new secret or new settings the panel skips that check: the server reads the
files once, for the copy, and a copy of the same files does not replace the saved one. A copy
made by an older panel (kept in the database, or a file encrypted whole) is still restored and is
replaced by a file in chunks with the next read of the service, even if the files have not
changed.

**A new proxy secret is in the copy at once.** **New secret** or new settings of a Telegram proxy
ask for a copy of the service right after the operation: the old copy holds the old secret, which
the panel no longer knows the proxy by.

**Keys issued just now are in the copy too.** Besides the hourly copy, a key issued, disabled,
enabled or revoked asks for one more copy of its protocol about 30 seconds later; the key
operations of those 30 seconds share it. If the server does not answer at that moment, the last
copy stays and the next hourly copy catches up. In a server group each member the change reached
gets such a copy, not only the leading server.

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

A restore that failed half way (the connection dropped while the files were put in, or the panel
stopped right after creating the container) leaves a container with keys of its own. The panel
labels the container when it creates it and marks it done only when the files are in, so
**Restore** run again removes such a container and installs the service anew instead of
refusing it. A container the panel did not
leave this way is still never overwritten.

## AmneziaWG snapshots around a group

Before a server joins a group (if it has clients), before it leaves one and before a failed join
is undone, the panel keeps a snapshot of its AmneziaWG files: the server key, the preshared key and the clients. Snapshots are
never overwritten and are listed with their date, reason, port and number of clients under
**Backup** on the server card. **Delete** removes a snapshot you no longer need: only the record
in the panel goes, the server is not touched, and the audit journal keeps the deletion.

**Restore** next to a snapshot starts a background operation: the panel takes the server lock,
saves the current files as a new snapshot ("before restoring a snapshot"), puts the snapshot files
in place (`awg0.conf` last) and restarts the interface. If the interface does not come up, the
old files come back. The panel then reads the server key and the clients again: keys of the
snapshot are active and connect again, keys issued after the snapshot are revoked, and clients
the panel did not know appear as imported keys.

A snapshot taken "before leaving a server group" or "before taking a joining server out of its
group" holds the files of the group: after the restore
the server runs with the group key again, and the panel warns about it before starting. Peers
that are not clients of this server are taken out of `awg0.conf` and `clientsTable` before the
files are written: a peer whose key lives on another server (usually a key of the group's leading
server) and the panel's service peers, such as the relay probe. The operation log shows how many
peers were left out, and the number of clients on the snapshot card does not count them. A key that comes back to
a user on a plan is turned off by hand when the plan does not allow this server or protocol or
the user already has more keys than the plan allows (the newest of the returned keys go off).
Owners of returned keys get a "keys updated" notification.

A group member does not restore a snapshot: take the server out of the group first. The restore
is also refused while the protocol listens on another port than in the snapshot: change the port
back in the protocol settings.

## Full recovery

When both the panel and a VPN server are lost:

1. Restore the panel from a panel archive, or from its backup and the saved `.env`
   ([Restoring the panel](#restoring-the-panel)).
2. For every lost VPN server follow [Restoring a protocol](#restoring-a-protocol-on-a-new-vps).
3. Open **Keys → Differences with servers → Check now**: if the panel and the servers still differ,
   the differences are listed there ([Keys](Keys.md#differences-with-servers)).
