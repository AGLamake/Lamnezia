# Protocols

A protocol instance is one Amnezia container on a server with its port, parameters and clients.
The panel supports **AmneziaWG 2.0** (`amnezia-awg2`), **AmneziaWG legacy** (`amnezia-awg`) and
**WireGuard** (`amnezia-wireguard`), **XRay** (`amnezia-xray`, VLESS Reality), **OpenVPN**
(`amnezia-openvpn`) and **IPsec** (`amnezia-ipsec`, IKEv2). Other containers are shown on the
server page as «found, not supported».

A protocol gets into the panel in one of two ways:

- **import** from a server set up in the Amnezia app ([Servers and import](Servers-and-import.md#import-from-a-server-set-up-in-the-amnezia-app));
- **installation** by the panel: the **Install** button on the server page
  ([below](#installing-from-the-server-page)), or a
  [restore on a new VPS](Backup.md#restoring-a-protocol-on-a-new-vps).

## Installing from the server page

**Install** next to **Import from the server** opens the list of what the panel can install
(administrators only): **AmneziaWG 2.0**, **AmneziaWG (legacy)**, **WireGuard**, **XRay (VLESS Reality)**, **OpenVPN** and **IPsec**, then the services and proxies. Something
already on the server cannot be
chosen again, and neither can a protocol removed with its backup kept: restore it from the backup.
The port may be left empty: AmneziaWG then gets the first free UDP port up to 9999 (from `5542`
up, so 2.0 and legacy on one server get different ports; see [Ports](Ports.md)), the others the port
of the Amnezia app (`51820/udp` for WireGuard, `443/tcp` for XRay, `1194/udp` for OpenVPN; IPsec always uses `500/udp` and `4500/udp`; the services and
proxies are described [below](#services-amneziadns-sftp-website-in-tor)). A port above 9999 gets a
warning in the install and settings forms.
The panel refuses a port it knows to be taken: another Amnezia container or the reachability probe.

As in the Amnezia app, the list is split into **VPN protocols** and **Services** (AmneziaDNS, SFTP,
website in Tor, proxies), and each entry has a short description of how it works and what it is
for; the form shows the description of the chosen one. The server page groups its containers the
same way, with containers the panel does not know under **Other containers**. The names and
descriptions are the ones of the app (`apps/protocols/catalog.py`).

The installation is an operation with steps, as the Amnezia app performs them: check the system,
wait for other operations on the server, prepare the server, install `conntrack` (only for the
containers that need it), check that the port is free (`lsof`; for TCP
only a listening port counts), build the image from the Dockerfile of the app, start the container,
configure it with `configure_container.sh` and start it with `start.sh`. The scripts are the ones of
the app with the same variables (`$CONTAINER_NAME`, `$DOCKERFILE_FOLDER` and the protocol ones).
At the end the panel reads the server back, as the import does: the protocol appears on the server
page marked as installed. The installation is written to the audit log.

A new protocol or service is added to the panel as one installer class registered in the installer
registry (`apps/protocols/services/installer.py`, AmneziaWG 2.0 in `awg_installer.py` is the
example) and an entry with its name, group and description in `apps/protocols/catalog.py`; the
page lists it without other changes.

## Preparing a new server

Before installing a protocol on a clean server, the panel prepares it the same way the Amnezia app
does. Only Debian and Ubuntu are supported. The panel:

- installs Docker (`docker.io`) and the `psmisc`, `lsof` packages, plus `apparmor` when AppArmor is on;
- creates the Docker network `amnezia-dns-net` (`172.29.172.0/24`) used by Amnezia containers;
- adds firewall rules for Docker and **blocks incoming ping** (ICMP echo), like the Amnezia app;
- applies the network tuning of the Amnezia app (`sysctl`, until the next reboot). Settings the
  kernel lacks are skipped and listed in the operation log.

Each step checks the server first, so running the preparation again is safe.

### Installing AmneziaWG 2.0

The panel installs the protocol with the scripts of the Amnezia app, so the server then opens in
the app as if it had been installed there:

- checks that the kernel is Linux 4.14 or newer and that the UDP port (a free one up to 9999 by default) is free;
- builds the `amnezia-awg2` image (downloads `amneziavpn/amneziawg-go` from Docker Hub) and starts
  the container;
- generates the server keys on the server and the obfuscation parameters (Jc, S1–S4, H1–H4, I1 and
  the AmneziaWG 2.0 timings) the same way the app does; client subnet `10.8.1.0/24` by default;
- creates an empty client list (`clientsTable`).

If the container `amnezia-awg2` already exists (installed by the app or by an earlier attempt), the
panel does not rebuild it and never overwrites its `awg0.conf`, `clientsTable` or `start.sh`: keys
and clients stay. A failed installation can be retried.

### AmneziaWG 3.1: which clients work

The panel installs new servers with the AmneziaWG 3.1 parameters right away: `HeaderProtectionKey`,
timers, `RandomTrailers`, `DisableCookies`, and `PersistentKeepalive = 25-35` in the client profile.
Such a key (`.conf` or `vpn://`) is imported only by these app versions or newer:

| App | Android | iOS | Windows | macOS, Linux |
|---|---|---|---|---|
| Amnezia VPN | 5.0.1.5 | 5.0.2 | 5.0.1.5 | 5.0.1.5 |
| AmneziaWG | 3.1.20260814 | 3.1.4 | 3.1.0 | Linux: `amneziawg-tools` 3.1 |

An older version does not know the 3.1 lines: the key is not imported or the tunnel does not come up.
Amnezia VPN 5.0.0.5 (iOS: 5.0.1) and AmneziaWG for Android 3.0.1 know only part of the 3.1 lines
(without `RandomTrailers` and `DisableCookies`), so they do not work either. Ask users to update the
app before you hand out keys from a 3.1 server. AmneziaWG 2.0 servers work with every version since
the one that added AWG 2 support.

What the server does with the 3.1 parameters:

- An `amneziavpn/amneziawg-go` image older than 3.0 does not know `HeaderProtectionKey` and the
  timers, and a 3.0 image does not know `RandomTrailers` and `DisableCookies`. `awg-quick up` then
  fails with ``Line unrecognized: `HeaderProtectionKey=…'`` and `Configuration parsing error`. A new
  installation downloads a fresh image; a container installed earlier stays on the old one.
- With `HeaderProtectionKey` set, each of S1–S4 must be at least 12. Otherwise `awg-quick up` fails
  with `Unable to modify interface: Invalid argument` and the interface does not come up. Without the
  key there is no such limit.
- If the `amneziawg` kernel module is loaded on the host, `awg-quick` in the container uses it instead
  of `amneziawg-go`. A module older than 3.0 silently drops the 3.1 parameters: the interface comes up,
  but 3.1 clients cannot connect. To check: `awg show awg0` on such a server has no
  `header protection key` line. Update the module to 3.1 or unload it.

### Moving an AmneziaWG 2.0 server to 3.1

The server page and the protocol settings show the AmneziaWG version next to the name: **version
3.1**, **2**, **1.5** or, for legacy AmneziaWG, **1.0**. The panel determines it from the parameters
the same way the Amnezia app does. The app calls a server below 3.1 outdated and offers to reinstall
the container, which removes all its clients; move it in the panel instead.

An AmneziaWG 2.0 card (installed by the panel or imported from the Amnezia app) has the
**Move to AmneziaWG 3.1** button. The background operation:

1. builds the image again with a fresh `amneziavpn/amneziawg-go` (`docker build --no-cache --pull`);
   the old container keeps serving clients meanwhile;
2. adds the 3.1 parameters to `awg0.conf` and raises S1–S4 to at least 12; the other parameters, the
   server keys, the PSK, the peers and `clientsTable` stay as they are, and so does the port;
3. replaces the container and checks that `awg show awg0` has the `header protection key` line;
4. updates the profiles of the keys and notifies their owners: each user imports the key again.
   With **Reissue the keys imported from the server** the keys created in the Amnezia app are
   reissued as well.

If the image is not built, AmneziaWG does not start, or the kernel module drops the 3.1 parameters,
the old container comes back and the server stays on 2.0 with the same keys. The header protection
key is not written to the operation log or the audit log. Clients are offline for a few minutes and
need an app version from the table above. Repeating the operation on a server already moved only
updates the panel.

### Moving the clients of legacy AmneziaWG to 3.1

Legacy AmneziaWG (`amnezia-awg`) cannot be moved in place: AmneziaWG 3.1 runs in another container,
`amnezia-awg2`. Its card has the **Move the clients to AmneziaWG 3.1** button. The background
operation:

1. installs `amnezia-awg2` with the 3.1 parameters on the port you choose, unless the server has it;
2. gives every client of the old container a new key there, with the same owner and name: all
   new peers in one change of `awg0.conf` and `clientsTable`;
3. notifies the owners (`keys_moved`): each user imports the new key from the personal account.

The old container and its keys keep working, so the clients switch when they are ready. Disabled
keys get disabled new keys and come back together. If a key is issued on the old container later,
run the move again: it gives new keys only to the clients that have none. An `amnezia-awg2` still on
2.0 must be moved to 3.1 first; one removed with its backup kept must be restored or the backup
deleted.

When every client has a new key, the card offers **Finish the move and remove the old container**.
It removes `amnezia-awg`, revokes its keys without notifying the owners who have new ones, and drops
it from the panel together with its backups.

### Installing AmneziaWG legacy and WireGuard

Both are installed with the scripts of the app (`server_scripts/awg_legacy` and
`server_scripts/wireguard`) and the defaults of the app; the kernel is not checked:

- **AmneziaWG (legacy)**: the `amnezia-awg` image (built on `amneziavpn/amnezia-wg`), UDP port
  as for AmneziaWG 2.0, the config `/opt/amnezia/awg/wg0.conf` with obfuscation parameters generated as for
  AmneziaWG 2.0;
- **WireGuard**: the `amnezia-wireguard` image (Alpine with `wireguard-tools`), UDP port `51820`,
  the config `/opt/amnezia/wireguard/wg0.conf` without obfuscation.

Client subnet `10.8.1.0/24`, an empty `clientsTable`; an existing container and its files are kept
as for AmneziaWG 2.0. Keys are issued, imported and counted for traffic the same way.

### Installing XRay (VLESS Reality)

XRay is installed with the scripts of the app (`server_scripts/xray`): the `amnezia-xray` image,
VLESS over TCP with Reality, TCP port `443` by default. The form asks for the **masking site**, the
site the server pretends to be for an observer (`www.googletagmanager.com` by default, as in the
app). Choose a large site that works over HTTPS; the field takes a domain name or an IPv4 address,
as the app does.

`configure_container.sh` generates the Reality key pair, the shortId and the client UUID of the
installer in the data volume `amnezia-xray-data`; the panel writes `server.json` the way the app
does, but with the access log off (`"access": "none"`): otherwise xray prints every
destination, and with the container log on they would stay in `docker logs`. A `server.json` already in the volume (an earlier attempt or the app) is kept with its
clients; a file that is not valid JSON stops the installation instead of being overwritten. The
keys are never written to the operation log.

XRay keys are issued, disabled, enabled, renamed, given to users and revoked like the others. XRay
has no live reload, so like the app the panel restarts the container after a change of
`server.json`: **every XRay client of the server reconnects** for a moment. A config that xray
rejects never replaces the working one, and if xray does not come up the old one is put back.
Renaming only changes `clientsTable` and does not restart. Differences with the server, traffic
counters and speed limits are not available for XRay yet.

Shadowsocks over XRay (SSXray) is not offered: the app has no server scripts to install it.

### Installing OpenVPN

OpenVPN is installed with the scripts of the app (`server_scripts/openvpn`): the `amnezia-openvpn`
image, UDP port `1194` by default, AES-256-GCM, SHA512, tls-auth and the client subnet
`10.8.0.0/24`, as in the app. The container makes an easy-rsa certificate authority, the server
certificate, `ta.key` and the first revocation list (CRL); on a small server the Diffie-Hellman
parameters take a few minutes. A CA and `server.conf` already in the container are kept with
their clients. The server address is given to the container as the app does it; a server added
by name is looked up once, at the installation.

The container has no data volume, as in the app: **removing the container destroys the CA**, and
every key issued on it stops working. Backups and restores do not cover OpenVPN.

A key is a client certificate: the panel makes the client's RSA key, the CA of the container
signs it, and the client goes to `clientsTable`. A key is exported as `vpn://` for the Amnezia
app and as an `.ovpn` file for other OpenVPN clients. **Disabling** marks the certificate revoked
in `pki/index.txt` and makes a new CRL; **enabling** takes the mark off, so the same profile works
again. **Revoking** runs `easyrsa revoke`, as the app does. openvpn reads the CRL only when it
starts, so after every change of the CRL the panel restarts openvpn: **every OpenVPN client of the
server reconnects** for a moment. If openvpn does not start with the new CRL, the previous one
comes back and openvpn serves with it; the change stays pending and the next one tries it again.
Renaming only changes `clientsTable`. Differences with the
server, traffic counters and speed limits are not available for OpenVPN yet.

### Installing IPsec (IKEv2)

IPsec is installed with the scripts of the app (`server_scripts/ipsec`): the `amnezia-ipsec` image
with libreswan, UDP ports `500` and `4500` (they cannot be changed, as in the app) and the client
subnet `192.168.43.0/24`. The container makes the certificate authority and the server
certificate in its NSS database; a CA and `ikev2.conf` already in the container are kept with
their clients. The server address goes into the server certificate; a server added by name is
looked up once, at the installation.

The container has no data volume, as in the app: **removing the container destroys the CA**, and
every key issued on it stops working. Backups and restores do not cover IPsec.

A key is a client certificate signed by the CA of the container, with an empty PKCS#12
password, as the app makes it. A key is exported as `vpn://` for the Amnezia app, as a `.sswan`
file for strongSwan on Android and as a `.mobileconfig` profile for iPhone, iPad and Mac.
**Disabling** puts the certificate into the revocation list (CRL), makes libreswan read it again
and ends the sessions of that client only; **enabling** takes it out of the list, so the same
profile works again. **Revoking** puts the certificate into the CRL and deletes it from the
database. Renaming only changes `clientsTable`. Differences with the server, traffic counters and
speed limits are not available for IPsec yet.

### Services: AmneziaDNS, SFTP, website in Tor

The services of the app are installed from the same list, with its scripts (`server_scripts/dns`,
`sftp`, `website_tor`) and under the same container names, so the app sees them on the server:

- **AmneziaDNS** (`amnezia-dns`) runs at `172.29.172.254` in the network `amnezia-dns-net`, the
  DNS address the VPN containers give their clients. It has no port to choose. A DNS container
  the app built does not turn the query logs off, so the server card marks it **built by the
  app** and offers **Rebuild**: the panel builds a new image from its own Dockerfile while the old
  container still answers, then swaps the containers; if the new one does not start, the old one
  comes back.
- **SFTP file sharing** (`amnezia-sftp`) listens on TCP `222` by default. The panel creates the
  user `sftp_user` with a random 16-character password, as the app does.
- **Website in Tor network** (`amnezia-torwebsite`) gets an onion address from Tor. It publishes
  no port. If Tor has not created the address yet, the installation stops and the container is
  kept: try again.

The server card shows the SFTP user name and password (behind **Show**) and the onion address. The
password is not in the page: **Show** asks the panel for it, and every look is written to the audit
log as *Show SFTP password*. The password and the address are stored encrypted and never written to the operation log. Server
import reads them from containers set up in the app. **Remove** deletes the container and its
image; files uploaded to SFTP and the site go with them. **Restart** (or **Start**), **Stop** and
**Log** work for services as for protocols (see below); a service installed without the container
log has none, install it again with **Keep the container log** ticked.

### Proxies: SOCKS5, MTProxy, Telemt, TProxy

The proxies of the app are installed from the same list with its scripts
(`server_scripts/socks5_proxy`, `mtproxy`, `telemt`, `tproxy`), under the same container names
and with the defaults of the app, so the app sees them and shows the same credentials:

- **SOCKS5 proxy server** (`amnezia-socks5proxy`, 3proxy) listens on TCP `38080` by default. The
  panel creates the user `proxy_user` with a random 16-character password; a container already
  on the server keeps its account. The card shows the address, the user name and the password
  (behind **Show**). Unlike the app, the panel turns off the 3proxy log: the script of the app
  writes every requested host to `/usr/local/3proxy/logs/3proxy.log`, and the panel keeps no log
  of the sites users visit. The app does not read that log and sees the proxy as before; a proxy
  set up in the app keeps its log until it is reinstalled from the panel.
- **MTProxy (Telegram)** (`amnezia-mtproxy`) and **Telemt (Telegram)** (`amnezia-telemt`) are
  MTProto proxies on TCP `443` by default, in the standard mode of the app (`dd` secret). The
  form also has the settings of the app: **Transport mode** FakeTLS (`ee` secret with the domain
  of a large HTTPS site, **FakeTLS domain**), a **Public host** for the links instead of the
  address of the server, a **Promotion tag** from `@MTProxybot`, **Additional secrets** (32 hex
  digits each, separated by commas) and NAT addresses for a server behind NAT (MTProxy needs the
  internal and the external one, Telemt only the external one). They are written where the app
  reads them: `/data/mtproxy-meta` of MTProxy and `/data/config.toml` of Telemt, so the app shows
  the same settings and links.
- **TProxy (Telegram WEB)** (`amnezia-tproxy`) is the web proxy of Telegram behind Caddy. The form
  asks for its **domain**, which must point at the server, and an **e-mail** for the Let's Encrypt
  certificate. It always publishes TCP `80` for the certificate and the HTTPS port (`443` by
  default). Before anything changes on the server the panel checks that the domain has an IPv4
  address and that it is the address of the server (the check uses the DNS of the panel), and that
  port `80` is free; otherwise the installation stops with an error that says what to fix.

The Telegram proxies keep their secret in the `<container>-data` volume: installing again over
the volume keeps the secret, so links already given out keep working. Their `configure_container.sh`
downloads files from `core.telegram.org` (Telemt also downloads its binary from GitHub); if the
secret is not made, the installation stops and the container is kept: try again. The card shows the
`tg://` and `https://t.me/` links (behind **Show the links for Telegram**) that add the proxy in
Telegram. Passwords and secrets are stored encrypted and never written to the operation log;
server import reads them from proxies set up in the app. **Remove** deletes the container, its
image and the data volume.

**New secret** under the links of MTProxy and Telemt makes a new secret the way the app does it
(`*_REGENERATE_SECRET`), keeps the other settings, restarts the container and reads the new links.
The links given out before stop working. The container must be running.

### TSO/GSO workaround

On some VPS upload through the VPN is several times slower than download, or a speed test from the
client stalls, while download is fine. The cause is the network interface of the AmneziaWG or
WireGuard container (usually `eth1`, the interface of its default route): with TSO/GSO on, the
kernel glues TCP packets into large ones and they are lost on the way. Turning TSO/GSO off on that
interface fixes it. Configs, keys and `clientsTable` do not change; clients do not need anything.

The install form of AmneziaWG 2.0, AmneziaWG legacy and WireGuard has **Turn TSO/GSO off in the
container (recommended)**, ticked by default. The server page has the **Upload speed through the
VPN (TSO/GSO)** block with the state and the buttons:

- **Turn on** installs `ethtool` (and `iproute2`, `util-linux` if they are missing), writes
  `/opt/lamnezia/offload/offload.sh` and the `lamnezia-offload.service` and
  `lamnezia-offload.timer` units, and turns TSO/GSO off at once. The timer runs 20 s after boot and
  then every 30 s: Docker recreates the interface with TSO/GSO on when the container restarts, and
  the timer turns them off again. It only changes something when an offload is on, so its runs do
  not fill the journal. The server needs systemd.
- **Check** reads TSO and GSO of every running container without changing anything. `on` means
  the interface glues packets, `off` that the workaround works, `-` that `ethtool` is not
  installed.
- **Turn off** stops and removes the timer, the service and the script and turns TSO/GSO back on.
  `ethtool` stays installed.

Turning the workaround on and off is written to the audit log. To check by hand on the server:

```sh
pid=$(docker inspect -f '{{.State.Pid}}' amnezia-awg2)
nsenter -t "$pid" -n ip -4 route show default          # the interface after "dev"
nsenter -t "$pid" -n ethtool -k eth1 | grep segmentation-offload
systemctl status lamnezia-offload.timer
```

## Protocol settings

Next to an AmneziaWG 2.0 protocol on the server page, **Settings** opens the form for the UDP port, the DNS of the
client profiles and the AWG parameters (Jc, Jmin, Jmax, S1–S4, H1–H4, I1–I5 and so on). Only an
administrator can open it. The values are checked before saving and the form will not take invalid
ones, for example:

- the port is not between 1 and 65535 or is taken by the reachability probe;
- Jmin is larger than Jmax, or a junk packet is over 1280 bytes;
- the H1–H4 ranges overlap, or S1–S4 give two kinds of packets the same size;
- `HeaderProtectionKey` is set while one of S1–S4 is below 12 (AmneziaWG would not start);
- I1–I5 use tags other than `<b 0x..>`, `<r N>`, `<rc N>`, `<rd N>`, `<c>` and `<t>`.

An empty parameter is left out of `awg0.conf`, as the Amnezia app does it. The parameters of
AmneziaWG 3.1 (`HeaderProtectionKey`, timers, `RandomTrailers`, `DisableCookies`) are a separate
group. On a 2.0 server the group reminds you to move the server to 3.1 first: its image may not know
these parameters.

A change of the port or the parameters breaks every client until it imports the key again, so the
form asks for a confirmation. **Apply** starts an operation:

- **Parameters only.** The `[Interface]` of `awg0.conf` is rewritten and AmneziaWG is restarted in
  the same container (`awg-quick down`/`up`). Peers, the server key and `clientsTable` stay. If awg
  refuses the new config, the old one is put back.
- **New port.** The container has to be recreated, because the port is published by docker. The panel
  checks the port is free, saves the server files, renames the old container to
  `amnezia-awg2-previous` and stops it, starts a new one on the new port and writes the files into
  it. If the new container does not start, it is removed and the old one comes back. Unlike the
  Amnezia app, the server keys and the clients are kept.
- **New client subnet.** Every client keeps its place in the new subnet: `10.8.1.5` of
  `10.8.1.0/24` becomes `10.9.0.5` of `10.9.0.0/24`. The panel rewrites the addresses in
  `awg0.conf` and `clientsTable` and recreates the container as for a new port; on failure the old
  container comes back with the old addresses. The subnet must be private, from `/16` to the
  current size (all clients must fit), and must not overlap another subnet of the server.
- **DNS only.** The server is not touched: DNS goes only into the client profiles.

Then the profiles of all issued keys are updated, and their owners get the **Keys must be imported
again** notification. Keys imported from the server have no private key in the panel, so their
profile cannot be updated: tick **Reissue the keys imported from the server** to revoke them and
issue new ones to the same owners.

The reissue is one **Reissue keys** operation (`keys.reissue`) per protocol, however many keys it has: the new clients are added and the old ones removed in one change of the server files. A key that could not be reissued keeps its old one, and the step log names it and why; the operation then fails, and a retry takes up only the keys left.

If an operation was interrupted while moving the port and `amnezia-awg2-previous` is left on the
server, the next change stops with a hint: compare the containers in `docker ps -a` and remove the
one that is not needed.

### Other protocols and services

Every protocol and service on the server page has **Settings**. The fields and checks are those of
the Amnezia app; each change is an operation under the server lock, and the page warns which clients
reconnect or must import their key again.

- **AmneziaWG (old), WireGuard**: the form above, with the parameters of their version (WireGuard
  has the port and the DNS only).
- **XRay (VLESS Reality)**: port, masking site, SNI (empty: the site; no `*`), flow and fingerprint.
  The panel offers only Reality over RAW, as the app installs it. The fingerprint and the
  `-udp443` flow go into the client profiles only. Any other change rewrites `server.json` the way
  the app does, keeping the clients, the traffic statistics and the speed limits. XRay has no live
  reload, so the panel reads the traffic counters first, then restarts the container. A new port
  gets a new container, as for AmneziaWG.
- **OpenVPN**: transport (UDP/TCP), port, VPN subnet (mask /24), auto-negotiated encryption, hash,
  cipher and TLS auth. The panel replaces the container and passes the certificate authority and
  the client certificates on to it. The keys keep working once their profile is updated, and every
  client reconnects. A new subnet clears the saved client addresses (`ipp.txt`). If the new
  container fails, the old one comes back with all its clients. **Additional settings** are those
  of the app: **Block DNS requests outside of VPN** (`block-outside-dns`, Windows clients of the
  Amnezia app only) and extra lines for the client profile and for `server.conf`. The lines cannot
  contain `$`, `` ` ``, `\` or a line `EOF`, because the app passes them through a shell script.
- **SOCKS5**: port, user name and password. Only Latin letters, digits and `_` are allowed, because
  the app reads the 3proxy account with `\w+`. The password goes to the operation encrypted and is
  not written to the audit log.
- **MTProxy, Telemt**: the port and the settings of the install form (mode, Fake TLS domain, public
  host, tag, extra secrets, NAT). The secret stays in the data volume, so the links change only with
  the port, the domain or the mode.
- **TProxy**: port, domain and the certificate e-mail (empty: the one given at the install).

On the same port the proxy runs `configure_container.sh` with the new settings and restarts; the
panel saves the settings files of the data volume first, and if the script fails, puts them back
and restarts the proxy with the old settings. A new
port gets a new container on the same data volume, and on failure the old one comes back with its
old settings.

IPsec, AmneziaDNS, SFTP and Tor have no settings in the app either. Their page explains why and
shows what the app shows: the IPsec ports, the DNS address, the SFTP account, the onion address.

## Container: restart, stop, log, removal

Under the protocol on the server page there are actions for its container. Each one runs as an
operation; its page shows the steps.

- **Restart** restarts the container, or starts a stopped one, and waits for the VPN interface to come up.
  Clients reconnect in a few seconds; traffic counters on the server start from zero, the panel
  keeps counting.
- **Stop** stops the container: clients cannot connect until it is restarted. The keys and the
  protocol stay in the panel.
- **Log** shows the container state and the last 200 lines of `docker logs`. The Amnezia app starts
  its containers with logging turned off (`--log-driver none`), so such a container has no log: the
  operation says so and shows only the state. To keep the log, tick **Keep the container log** in the
  protocol settings: the panel recreates the container on the same port with `--log-driver json-file`
  (up to 3 files of 10 MB), keys and clients stay and their profiles do not change. A restore from a
  backup starts the container with the same choice.
- **Remove** opens a page that asks to type the server name. The container and its image are removed
  from the server with `remove_container.sh` of the Amnezia app (containers that keep their data in a
  `<container>-data` volume lose the volume too): the configuration and the clients are lost. By default
  the panel keeps the protocol marked **container removed**, with its keys and backup: the keys stop
  working until **Restore on this server** in the **Backup** section brings the protocol back with the
  same clients. Before the removal the panel copies the files of the VPN protocol; the page warns
  when the protocol has no copy yet and, unlike AmneziaWG 2.0, cannot come back from the panel keys
  alone ([Backup](Backup.md#copies-of-the-server-files)). Tick **Delete all backups?** to revoke the keys instead: the panel forgets the protocol
  and its backup, and the owners of the keys get the **Keys revoked** notification. A protocol already
  marked removed is deleted without connecting to the server. A container already gone from the
  server is no error.

Restart, stop and removal wait for other operations on the server and are written to the audit log.
