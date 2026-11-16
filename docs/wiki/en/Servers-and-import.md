# Servers and import

A server is a VPS that the panel manages over SSH. Servers are visible to administrators only:
**Servers** in the side menu.

## Adding a server

**Servers → Add a server**, then fill in:

- **Name** and **location** (country or any label, e.g. «Germany, Hetzner») — to tell servers apart.
- **Host** — a domain name or an IP address, without `root@`, port or `ssh://`; **SSH port** — 22 by
  default.
- **SSH user** — `root`, or a user with sudo **without a password** (`NOPASSWD` in sudoers), as the
  Amnezia app requires.
- **Login with** — an SSH key (OpenSSH or PEM: Ed25519, ECDSA, RSA; plus the passphrase if the key has
  one) or a password. A key is recommended.
- **VPS paid until** — optional, a reminder about the hosting payment. The server card shows how
  many days are left, or how many days the payment is overdue.
- **Under maintenance** — users see the server as under maintenance on their
  [status page](Portal.md#servers); new keys are not issued on it in the portal, the bots and the
  panel, while existing keys keep working and can be reissued. The server list marks it with a
  *maintenance* badge.

The panel stores the password, key and passphrase encrypted with `LAMNEZIA_ENCRYPTION_KEY` and never
shows them again: when editing a server, leave these fields empty to keep the saved values.

### New address of a server

The `vpn://` link of every key carries the server address. When the **Host** of a server with
keys changes, the panel opens **Keys after the address change**: the keys issued by the panel get
the new address in their profile at once (nothing changes on the server), and the keys imported
from the server, which have no profile in the panel, are reissued if **Reissue the keys imported
from the server** is ticked. The owners get the **Keys must be imported again** notification. **Later** keeps the keys as they are; the change is written to the audit log
(`protocols.readdress`). The reissue is one `keys.reissue` operation per protocol of the server
([Protocols](Protocols.md)).

A new host or SSH port also makes the panel forget the SSH fingerprint saved for the old one; the
audit log keeps it (`servers.forget_host_key`, with the old host and port). The next check trusts
the key of the new address.

**IPsec.** The IPsec server certificate is issued for the server IP, and clients refuse a server
whose IP is not in it. If the server has IPsec, the page warns about it and offers **Reissue the
IPsec server certificate for the new address** (ticked by default). The server card shows the IP
the certificate is issued for, a warning when the server address is another IP, and a **Reissue**
button. The reissue (`protocols.ipsec_reissue`) is a background operation under the server lock:
the same CA signs a certificate for the server IP (resolved if the server is given by a name),
ikev2.conf names it, IPsec restarts and the old certificate is deleted. The CA and the client
certificates stay, so the keys issued by the panel only get the new IP in their profile; the
owners are notified and import the keys again. The keys imported from the server are reissued
only if that box is ticked. A failed operation can be run again: finished steps are not repeated.

## Server check

After the server is added, and after its address or credentials change, the panel checks it in the
background. The check changes nothing on the server:

1. **Connect** over SSH. On the first connection the panel remembers the server host key; if the key
   changes later, the connection is refused (the server was reinstalled or the connection is
   intercepted). The server card shows the saved fingerprint. If the VPS was reinstalled, press
   **Forget** next to it: the panel forgets the fingerprint, writes this to the audit log
   (`servers.forget_host_key`, with the old fingerprint) and checks the server again, trusting
   the key it shows now.
2. **Root access**: the user is root or has passwordless sudo.
3. **Operating system** and kernel.
4. **Docker** version.
5. **Free disk space** in `/var/lib/docker` (or `/` before Docker is installed).

If the connection or root access fails, the server gets the status «check failed», and its page shows
the reason and what to do, e.g. «The server rejected the login. Check the user name and the password or
key». Fix the server settings or the server itself and press **Check again**.

The other findings are only warnings, the server is still «ready»:

- the OS is not Debian or Ubuntu — the panel cannot install protocols there (importing an existing
  server still works);
- Docker is not installed — the panel installs it together with a protocol;
- less than 1 GB is free — a protocol may fail to install.

Every check is recorded in **Operations** with its steps and log.

## Import from a server set up in the Amnezia app

On the server page press **Import from the server**. The panel:

1. lists the running Amnezia containers (`docker ps`, as the Amnezia app does);
2. for **amnezia-awg2** (AmneziaWG 2.0) reads `awg0.conf`, `clientsTable` (both the current and the
   old format) and the preshared key, and saves the protocol with its port, subnet and parameters;
   **amnezia-awg** (AmneziaWG legacy) and **amnezia-wireguard** are read the same way from `wg0.conf`;
   for **amnezia-xray** (VLESS Reality over TCP) it reads `server.json`, `clientsTable` and the
   Reality public key; the client the app made for itself while installing becomes a key too,
   named «Installer's client»; the panel never revokes or disables it, since the app needs it;
   for **amnezia-openvpn** it reads `server.conf`, the certificates in force in `pki/index.txt`,
   `clientsTable` and the CA certificate; the server's own certificate and revoked ones are skipped;
   for **amnezia-ipsec** it reads `ikev2.conf`, the client certificates of the NSS
   database that are not in the revocation list, `clientsTable` and the CA certificate;
   for the proxies it reads the SOCKS5 account from `3proxy.cfg` and the secret of a
   Telegram proxy from `/data/secret` with its mode, domain and public host, as the app;
3. turns every client into a key **without an owner**, named as in the Amnezia app.

The import only reads: it does not change files on the server and does not even take the panel's
server lock, so the Amnezia app keeps seeing the same clients. Imported keys have no private part
(only the client device has it): they cannot be shown or sent again, only given to a user, disabled or
revoked — see [Keys](Keys.md). Imported XRay keys can be shown: the panel builds their profile
from `server.json`.

Other containers (XRay with another transport and so on) are listed as «found, not
supported».
What can be done with an imported protocol: [Protocols](Protocols.md).

Importing again is safe: the protocol settings are refreshed and only clients the panel does not know
yet are added; keys already in the panel, with their names and owners, stay as they are. After the
import keys can be issued on the server, and its traffic is collected.

## Monitoring

Every `LAMNEZIA_MONITOR_INTERVAL` seconds (a minute by default) the `beat` service reads every server
with one SSH command: CPU load, memory, disk space of `/`, time since boot and the state of the Amnezia
containers (`docker ps -a`). The reading changes nothing on the server. Each server is read by a
background job of its own, so a server that does not answer does not delay the others.

[![Server page](../../images/server-en.png)](../../images/server-en.png)

- **online** — the server answered within the last two intervals. One missed reading is not an outage
  yet: a server is shown **offline** after two intervals without an answer, together with the reason
  (e.g. «The server did not answer in time») and when it last answered.
- **no data yet** — the server was added and has not been read yet.
- CPU load is measured between two readings, so the first reading after adding a server or after a
  reboot shows «—».
- An Amnezia container that is not running (stopped, exited, restarting) is listed in red. The
  server card shows the state of every protocol and service container from the latest reading
  next to its buttons; a stopped container has **Start** instead of **Restart** and **Stop**.

The servers are shown as cards on the **Overview** (moderators see the servers of their zone, without links to the
server pages), the status is also in the server list. Above the cards the overview shows the key
numbers (servers online, active keys, clients, online now), the traffic for 7 days and the
**Needs attention** list: servers that do not answer, server groups where no member answers the
relay at its last reading and relays whose state cannot be read (both with a link to the group or
server page, administrators only), VPS payments due within 7 days or overdue (administrators only),
operations failed in the last day, unread messages, applications and article
suggestions waiting, hidden materials with a broken link. The same counters are next to the menu
items. The crossed-out eye next to an item hides it until tomorrow (for this staff member, in their
session); **Show hidden until tomorrow** under the list brings everything back. Instead of the
servers online a moderator sees **My clients** and **Access ends in 7 days** — their clients whose
access is due for renewal soon. The server page shows the latest reading and the last
7 days: availability (the share of answered readings), average CPU, peak memory and least free disk per
day. Older readings are deleted automatically.

## Reachability probe

A browser cannot send arbitrary UDP, and AmneziaWG ignores packets from strangers, so a browser
cannot probe the AWG port itself. The **Reachability probe** section of the server page solves this:
**Install** puts a small STUN responder (the `lamnezia-probe` container) on a separate UDP port,
3478 by default. Users' browsers ask it over WebRTC and show a hint when the server does not answer
from their network (see [Portal](Portal.md)).

- Port: 1024–65535, not the SSH port and not an Amnezia container port. Open it in the firewall of
  the server and of the hosting provider **before** installing.
- After starting it the panel sends a STUN request to the server itself. Without an answer the
  container is removed and the operation fails with a hint to open the port: otherwise every user
  would see "unreachable".
- The container is unprivileged, read-only and has no logs (`--log-driver none`): the responder
  keeps no record of who asked.
- **Remove** takes down the container and the files in `/opt/lamnezia/probe`. Without the responder
  users' browsers do not probe the server.
- The server monitor watches the container. When a reading finds it stopped, the section warns
  about it and users' browsers stop probing the server until it runs again, so nobody sees a
  false "unreachable". The probe does not count as a stopped VPN container for users.

The probe tells whether UDP reaches the host, not the AWG port itself: if the provider blocks only
the AWG port or detects the protocol by DPI, it still shows "reachable". It also shows "unreachable" when only the probe port is closed
in a cloud firewall, or when the client's network blocks STUN; with WebRTC turned off in the
browser the result is "unknown".

## Server groups

**Servers → Server groups** joins AmneziaWG 3.1 servers into a group whose members are clones of
its leading server: the same server key, settings, port, subnet and clients, so one key works on
each of them. A server is either standalone or a member of one group.

- **New group**: pick a standalone AmneziaWG 3.1 server as the leading one. Nothing changes on it;
  the group takes its port.
- **Join the group**: a standalone AmneziaWG 3.1 server on the same port and subnet takes the files
  of the group in a background operation. If it has its own clients, the panel warns how many keys
  stop working, keeps a snapshot of its files first and revokes those keys, and users who had keys
  on it get keys of the group. A clean server joins without a warning.
- **Install and join**: a server without AmneziaWG gets AmneziaWG 3.1 installed straight with the
  files of the group, with no standalone step and no snapshot. The UDP port of the group must be
  free: a port the panel knows to be taken hides the button, and the installation checks the port
  on the server before it builds anything. A server that runs a relay is not offered.
- **Leave the group**: the server keeps a snapshot of its files and gets a clean AmneziaWG with a new
  key and no clients. The snapshot taken when it joined can be put back after leaving
  ([Backup](Backup.md#amneziawg-snapshots-around-a-group)).
  If the leave operation fails (the server does not answer, a write fails), the member is marked
  "leave failed" and gets two buttons: **Retry leaving** runs the leave again and brings the
  server to a clean standalone AmneziaWG; **Keep in the group** takes it back as a lagging member —
  the relay skips it until the catch-up copies the files of the group onto it. The catch-up starts
  at once, without waiting for the periodic check.
- **A failed join**: if the join operation fails, the member stays "joining" and is marked
  "join failed". **Retry joining** runs the join again and makes the server a clone of the group.
  **Take out of the group** removes the member: a server that still runs its own key keeps its
  files untouched; one the join already gave the files of the group gets back the snapshot taken
  before the join, or a clean AmneziaWG with a new key if it had no clients. A server that runs a
  key of neither is refused untouched.
- **Make leading**: another member takes the lead. A background operation first compares its files
  with those of the group and refuses if the server differs from the group. Keys of the group,
  with their traffic history, pass to the new leading server, and new keys are issued on it; if
  the group has no relay, the address in the keys changes to the new leader's and their owners are
  notified. If the former leader does not answer, the handover goes on without it and it is marked
  lagging. A lagging member has no such button; the handover waits for key operations of the group
  to finish, and the group page lists those operations with links while they run.
- The leading server cannot leave — make another server the leading one first: **Leave the group**
  on the leading server shows the error with a **Make … leading** button for every member that may
  take the lead. A group with only its leader left can be deleted, and the leader stays as it is.
- **Edit** changes the name, the domain and the relay of the group. A server of the group cannot be
  its relay, and the relay server of the group is not offered to join it.
- A join or an install under way is shown above the members with a link to its operation.
- The **Server groups** item of the menu counts the groups to look at: no member answers the relay,
  a member lags, or a member is stuck after a failed leave.

Keys of a group are issued on its leading server. Issuing, disabling, enabling and revoking a key
run on every member that answers, without waiting for the others. A member the panel cannot reach
is marked **lagging**: the relay sends no clients to it. The member list says why a member lags:
it was unreachable during a change, it missed a change (it joined or caught up meanwhile, or did not
take new settings), its files differ from the leading server, or it stayed after a failed leave. Every `LAMNEZIA_GROUP_CATCH_UP_INTERVAL`
seconds the panel checks the lagging members and, once a server is online again, copies the group
files onto it in the background operation "Catch up with the server group": its keys then match the
group, revoked ones are gone, and it is back in the relay list. When a lagging member already has
every key of the group and only the client list differs, catch-up rewrites only `clientsTable`
without restarting AmneziaWG. Members get the client record in `clientsTable` with the same date
as the leading server, and a batch reissue of keys changes each member in one config update. If no member answers, the operation
fails and changes nothing.
Every `LAMNEZIA_RECONCILE_INTERVAL` seconds the panel compares the peers and the client list of
each member with the leading server. A member where a peer was added in the Amnezia app or removed
by hand is marked lagging too, and catch-up gives it the group files back. The keys of the panel
stay as they are: the comparison with the panel (drift) reads only the leading server.

**Protocol settings** on the group page change the DNS, the client subnet and the AmneziaWG
parameters of every member at once; the card of a member shows them read only with a link to the
group. The port stays the port of the group, and the container log is set on each server. The
background operation "Change the protocol settings of a group" locks every member that answers,
applies the settings on each and keeps them clones. A member that does not answer or does not take
the settings is marked lagging and catch-up gives it the new settings, moving its container into a
new subnet if needed. If no member answers, nothing changes. A DNS-only change does not touch the
servers. A settings change and a handover of one group do not run at the same time.

**Short timers** on the same page set `KeepaliveTimeout = 3-5` and `RekeyTimeout = 2-3` for the
group. A client notices the switch of the [relay](#relay) by these timers: with them it reconnects
in 7-9 seconds instead of 18-22 with the timers of a new server. The cost is more handshakes on
mobile networks with losses, so the setting is off by default. Like any parameter change it
restarts the protocol on every member, and the owners of the keys are told to import them again.
Turning the setting off brings back `5-15` and `3-7`; while the checkbox is left as it is, the two
fields keep what was typed in them.

**Plans** give a group as a whole: the **Server groups** field of a [plan](Users-plans-limits.md#plan)
lets its users get the key of the group, which works on each member whoever leads the group. The
portal, the bots and the admin interface name such a key by the group. Servers of a group are not
offered among the plan servers: a plan that named a server before it joined a group gets the group
instead, and when a group is deleted its plans get its last leading server back.

### Relay

Clients of a group connect to the relay, which forwards their traffic to a live member. Any added
server can be the relay, including the panel's own machine.

1. In the group settings, pick the server in the **Relay** field.
2. On that server's card, open **Relay** and press **Install**. A background operation checks the
   OS, adds the service peer "Lamnezia relay" to every member, builds the `lamnezia-relay`
   container and publishes the ports of the groups as UDP.

The port of a group is checked before the installation: ports 80 and 443 belong to the panel's web
server, and a port another program or container listens on is refused with a clear error. A port
taken by AmneziaWG on the relay server gets its own hint: if that AmneziaWG is a member of the
group, pick a relay on another server, otherwise change its port.

The relay and a member of its group cannot share a machine: every member listens on the port of the
group, the very port the relay publishes. The panel refuses it before anything is installed: the
relay server cannot lead or join the group, nor get AmneziaWG of the group, and installing or
updating a relay whose server runs a member of its group fails before the panel connects. The
periodic sync does not retry such a relay. The container runs in its own network, without
`network_mode: host`, and publishes only the ports of the groups.

The agent inside the container probes the members in order every few seconds with a new handshake
through the service peer and sends clients to the first live one. Before a switch the candidate is
probed again, the `nftables` rules change in one transaction, and old connections are dropped. The
agent does not need the panel: switching works even while the panel is down. Its choice lives in
the `lamnezia-relay-data` volume, so neither a restart nor a new container after a port change
moves clients off the active member. The address of the service peer is never given to a key,
even on a member that does not have the peer yet.

To service the active server, press **Switch to this server** next to another member on the
group page. The panel asks the agent to send the clients there; the agent probes that member once
more and switches the same way as on a failure, or refuses and keeps the active one when the
member does not answer. Every client of the group loses its connection for about 20 seconds. The
switch is a background operation; it ends with the notification of a switch. A relay agent of an
older release does not answer the request: update the relay on the server card first. The agent
then goes on switching by itself as before: a planned switch does not pin the group to the member.
Under the member list the group page shows the last planned switch: when it was started, its
status with a link to the operation and, if it failed, the reason.

When the members, their order, the lagging ones, the port of a group, the address of a member
server or the keys and parameters of a member change, the panel updates the relay by itself right
after the change. Every `LAMNEZIA_RELAY_SYNC_INTERVAL` seconds it also checks what no change in the
panel shows: a member name in DNS that now points to another IP address updates the relay too.
**Update** on the card does it at once. A relay the update could not help is left as it is, and
the card says why: a member of a group runs on the relay server itself, or the name of a member
server does not resolve.

The card also sets how often the agent probes each member (5 seconds by default) and after how
many failed probes in a row it moves a group to the next member (3 by default). **Save the
settings** writes them to the relay by a background update, without a reinstall; a shorter
interval or fewer failures switch sooner but react to short losses too. **Remove** drops the container, the image and the volume, then takes the service peers off
the members. A member that does not answer is marked lagging and loses the peer on its catch-up;
when no member of a group answers, the panel keeps the peer. Keys of the groups go back to the
address of their leading servers, and their owners are told to import them again.

Every `LAMNEZIA_RELAY_MONITOR_INTERVAL` seconds the panel reads the state the agent writes. The
group page shows for each member whether the relay reaches it, which one is **active** now and
whether the panel itself reaches it over SSH, with the time of the last switch. The relay card
shows the channel load and where the traffic of each group goes, with charts of the load from the
clients and to them below: the last day by hour and the last week by six hours, with the peak
average rate. Under them a full-size chart stacks both directions in bytes per second; the
**24 hours** / **7 days** switch picks the period, and a tap on a bar shows its numbers. The load readings are kept and thinned like the server readings
(`LAMNEZIA_MONITOR_HISTORY_DAYS` and `LAMNEZIA_MONITOR_DETAIL_DAYS`). A switch, a member the relay
cannot reach, a group with no member left, a relay whose state is not read for two intervals and
a member lagging longer than `LAMNEZIA_GROUP_LAG_ALERT` come as notifications
([Notifications](Notifications.md)). Clients of a group are seen by the members at the relay's
address; the panel shows no client addresses.

### Keys of a group behind the relay

Once the relay of a group is installed, the `vpn://` links and `.conf` files of its keys carry the
**Domain** of the group, or the relay address when the domain is empty, and the port of the group.
Keys the leading server had before move to that address in the same operation, and their owners,
like the owners of keys revoked when their server joined, get the notification **Keys updated for
a server group** in the personal account channel: they import the key into the Amnezia app again,
once. A key that keeps the old address goes on working while the leading server is up. A key
issued while the relay is being installed takes the address it has when the key is saved.

A new **Domain** on the edit page moves the keys to it at once, with the same notification. When
the relay is taken off the group, or removed from its server, the keys go back to the address of
the leading server. With another relay the keys move once that relay runs the group, on its next
update.

The domain needs only an A record with the relay address. The group page warns when the domain
does not resolve, points elsewhere, or has an AAAA record: the Amnezia app on a computer may take
the IPv6 address and miss the relay. DNS is checked in the background and the result is kept for
10 minutes: the page does not wait for DNS, and shows a new domain or a fixed record on the next
visit after the check.

Traffic, online status and limits of a group key are summed over all members: whichever server the
relay sends the client to, its traffic counts for the same key.

## Server access

The panel connects to servers over SSH as `root` or as a user with passwordless sudo
(`NOPASSWD` in sudoers), like the Amnezia app does. On the first connection the panel remembers the
server host key. If the key changes later, the connection is refused: after reinstalling a server,
delete its record in the Django admin site `/admin/` → **Ssh → Known hosts**.

While the panel changes a server, it holds the lock `/opt/amnezia/.lamnezia.lock`, so two panel
operations never edit one server at the same time. The Amnezia app does not know this lock:
do not edit the same server in the app and in the panel at the same moment.

## Operations

Everything the panel does on servers runs in the background (the `worker` service) and is written
to the operation journal: **Operations** in the admin interface. The page of an operation shows
its steps and their log while it runs. If an operation fails, it shows the failed step, what went
wrong, what to do, and a **Retry** button that runs it again with the same parameters. The name of
the object under the operation title links to its page: the key for a key operation, the server for
the rest (a protocol or a service leads to its server).

The journal can be searched by the object name and filtered by status; the search and the page
stay in the URL, so a filtered list can be shared. While an operation on the page runs, the table
refreshes itself every few seconds.

**Servers** has a search by name, address or location and a status filter. The rows of the list and
the server cards on the dashboard have two quick buttons: **Check again** and **Edit**.

**Ctrl+K** (⌘K on a Mac), or the search button in the menu, opens quick search: type a part of a
server, user or key name and press Enter to open the first result, or walk the results with the
arrows. A moderator finds only their users and keys, and no servers.

Under every list (operations, audit journal, users, keys, notifications, payments) you can choose
how many rows a page shows: 25, 50 or 100. The choice is kept in the session and applies to every
list.

The theme button in the menu opens **Appearance**: light, dark or as the system, and **Page width** —
normal or wide. Every admin page has the same width; the choice is kept in the browser.

If the worker stops in the middle of an operation, the `beat` service marks it as interrupted after
`LAMNEZIA_OPERATION_TIMEOUT` seconds, and it can be retried.
