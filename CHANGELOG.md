# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Hints on the group page**: the member list says why a member lags (unreachable during a change,
  missed a change, files differ from the leading server, stayed after a failed leave). Joins and
  installs under way and the key operations that hold a handover are listed with links to their
  operations. **Leave the group** on the leading server offers **Make … leading** for the members
  that may take the lead. The relay server of a group is no longer offered to join it. The menu
  gets a **Server groups** item with a counter of groups to look at. Members that lagged before the
  upgrade show no reason until they lag again.
- **Relay settings and instant sync**: the relay card sets the probe interval and the number of
  failed probes before a switch; saving writes them by a background update, without a reinstall.
  A change of the members, the port of a group, a member address or member keys updates the relay
  right away instead of at the next minute. A member name in DNS that moves to another IP updates
  the relay at the next check, and the card says why a sync skips a relay. After the upgrade every
  installed relay gets one update.
- **Short timers of a group**: a checkbox in the protocol settings of a group sets
  `KeepaliveTimeout = 3-5` and `RekeyTimeout = 2-3` on every member and in the keys, so clients
  behind a relay reconnect in 7-9 seconds after a switch instead of 18-22. Off by default: on lossy
  mobile networks clients make more handshakes.
- **Planned relay switch**: **Switch to this server** on the group page sends the clients of a
  group behind a relay to the chosen member, for maintenance of the active one. The relay agent
  switches only if that member answers its probe right then, otherwise it keeps the active one;
  clients reconnect in about 20 seconds and `relay_switched` is sent. The agent goes on failing
  over by itself; an agent of an older release needs an update of the relay first.
- **Protocol snapshots**: the list shows the port and the number of clients of each snapshot, and
  an administrator can delete a snapshot. Restoring a snapshot taken before leaving a group warns
  first, does not add again peers whose keys live on another server, turns off returned keys over
  the owner's plan and tells the owners their keys changed.
- **Editing a server group**: the name, the domain and the relay of a group change on its page.
  A new domain moves the keys of the group at once, and a relay taken off or removed sends them
  back to the leading server; owners are told to import their keys again.
- **Server groups in plans**: a plan gives whole server groups, and its users get the key of the
  group, named by the group, on each member whoever leads it. Servers of a group are no longer
  offered one by one; plans that named such a server get its group, and a deleted group leaves its
  leading server in its plans.
- **Relay trouble on the dashboard**: the "Needs attention" list of an administrator names the
  server groups where no member answers the relay at its last reading and the relays whose state
  cannot be read, with links to the group and server pages. It uses the stored reading, without SSH.
- **Recovery notifications** paired with the trouble events: `server_up`, `member_reachable`,
  `group_reachable`, `relay_up` and `member_caught_up` come once when the trouble is over, only
  after `server_down`, `member_unreachable`, `group_unreachable`, `relay_down` or `member_lagging`.
  The panel remembers an outage or a lag apart from notifications, so `server_up`, `relay_up` and
  `member_caught_up` come even when no channel is subscribed to the trouble event.
- **Relay load chart**: the relay card shows the load from the clients and to them for the last
  day by hour and the last week by six hours. The monitor keeps every reading, thinned and deleted
  like the server readings; the history is listed in the Storage section of the statistics.
- **A failed leave of a server group** can be retried or undone on the group page: the retry
  brings the server to a clean standalone AmneziaWG, and "Keep in the group" takes it back as a
  lagging member that the catch-up brings to the files of the group.
- **Restoring a snapshot** of an AmneziaWG server from the backup section of its card: a background
  operation saves the current files as a snapshot, puts the snapshot files back with a rollback if
  the interface does not come up, and the panel reads the keys again, so the old keys connect.
  A group member is refused until it leaves the group.
- **Server groups**: AmneziaWG 3.1 servers join a group as clones of its leading server, so one
  key works on each of them. Joining and leaving run as background operations with a snapshot of
  the server files; a server is either standalone or in one group. A server without AmneziaWG
  joins by an installation straight with the files of the group, after a check that the port of
  the group is free.
- **Keys of a server group** change on every member that answers. A member that misses a change
  is marked lagging, left out of the relay list and caught up by a periodic operation
  (`LAMNEZIA_GROUP_CATCH_UP_INTERVAL`). Users of a joining server get keys of the group.
  After a key change every member it reached gets a fresh backup copy. A member whose peers or
  clients differ from the leading server (edited in the Amnezia app or by hand) is marked lagging
  every `LAMNEZIA_RECONCILE_INTERVAL`, and catch-up gives it the group files back.
- **Protocol settings of a server group** (DNS, subnet, AmneziaWG parameters) change on the group
  page and apply on every member that answers; a member that misses them lags and catch-up brings
  them, moving its container into a new subnet if needed. The card of a member shows them read only.
- **Relay of a server group**, installed with one button on any server, the panel's machine too.
  Its ports are checked before the installation; its agent probes the members through a service
  peer and switches clients to a live one by itself, even while the panel is down. The panel
  updates the relay when its groups change (`LAMNEZIA_RELAY_SYNC_INTERVAL`).
- **Relay monitoring**: the panel reads the agent state (`LAMNEZIA_RELAY_MONITOR_INTERVAL`). The
  group page shows the active member and who the relay and the panel reach; the relay card shows
  the channel load. New events: `relay_switched`, `member_unreachable`, `group_unreachable`,
  `relay_down` and `member_lagging` (`LAMNEZIA_GROUP_LAG_ALERT`).
- **Keys of a group go through the relay**: with the relay installed, their `vpn://` links and
  `.conf` files carry the domain of the group (or the relay address) and its port, and the group
  page checks the DNS records of the domain. Owners of moved keys and of keys revoked at a join
  get the new event `keys_updated`. Traffic, online status and limits of a group key are summed
  over all members.
- **Lead handover in a server group**: the "Make leading" button passes the lead to a member that
  is a clone of the group, checked before the handover. Keys of the group and their traffic
  history move to the new leading server, which then issues keys; the former leader may leave. A
  leading server that does not answer is left behind as lagging.

### Changed

- **Translations for developers**: `makemessages` and `compilemessages` run without `.env` and the
  keys, and a bare `manage.py makemessages` has the project flags built in, so it writes the same
  catalogs as `make messages`. A fresh clone needs one `compilemessages` before the tests.

- **A relay and a member of its group on one machine** are refused before anything is installed,
  with a clear error: the relay server cannot lead, join or get AmneziaWG of the group, and a relay
  whose server runs a member of its group is not installed, updated or retried by the sync. A
  group port taken by AmneziaWG on the relay server gets a hint on what to do.
- **Relay service peers and state**: a key issued while the relay is being installed never gets
  the address of the service peer. The agent keeps its choice in the `lamnezia-relay-data` volume,
  so a new container after a port change stays on the active member. Removing the relay takes the
  service peers off the members; one that does not answer loses it on its catch-up.
- **Server group members** get the clientsTable record of a new key byte for byte as the leading
  server writes it. Catch-up of a member that already has every key of the group rewrites only
  clientsTable and does not restart AmneziaWG. A batch reissue changes each member under one lock
  with one `syncconf` instead of one per key.

### Fixed

- A key of a server group issued while its relay was being installed kept the address of the
  leading server; it now takes the address the group has when the key is saved.
- **Relay install or update with no member of a group answering** no longer fails with an
  internal error: the service peer of that group is left for the next sync, and the operation log
  says why.

## [1.0.0] - 2026-11-13

The first release: a self-hosted web panel for Amnezia VPN servers, compatible with the Amnezia
app (`clientsTable`, server configs and `vpn://` links are kept in its format).

### Added

- **Servers**: add a VPS over SSH (password or key, stored encrypted), check it, import what the
  Amnezia app installed together with its clients, monitor CPU, memory, disk and containers,
  maintenance mode, VPS term reminders.
- **Protocols**: install, configure, restart, stop and remove AmneziaWG 2.0 and 3.1, legacy
  AmneziaWG, WireGuard, XRay (VLESS Reality), OpenVPN and IPsec from the interface; move
  AmneziaWG 2.0 servers and legacy clients to 3.1; reissue the IPsec certificate for a new IP.
- **Services and proxies**: AmneziaDNS, SFTP, a Tor website, SOCKS5, MTProxy, Telemt and the
  transparent proxy.
- **Keys**: issue, show as a `vpn://` link, QR code or config file, disable, revoke and reissue;
  clients added in the Amnezia app are picked up and differences with the server are shown;
  a subscription link for XRay.
- **Users and plans**: plans with traffic per period, total traffic, key count, access term and
  servers; personal limits; keys turn off at a limit and back on within it; bulk actions.
- **Moderators**: a zone of their own users and servers with quotas.
- **Registration**: by invitation, open, or open with approval; email confirmation and sign-in by
  email link.
- **Traffic and statistics**: per key, user and server, charts, top consumers, inactive keys,
  CSV export and a Prometheus endpoint; XRay speed limits per plan.
- **Client portal**: keys, limits, server status, install guides with links to Amnezia releases,
  a reachability check from the browser, support requests and a live chat; installs as an app.
- **Support and incidents**: inbox for staff, reminders, "server does not work" reports grouped
  into incidents.
- **Telegram and Discord**: users link their accounts and sign in with them; the bots give keys,
  status and guides, open support requests, bring the staff's answers and have staff commands.
- **Notifications**: email, Telegram, Discord and signed webhooks, with per-channel event routing.
- **Payments**: payment services as plugins (`LAMNEZIA_PLUGINS`), orders in the admin interface.
- **Security**: roles, two-factor login for staff, login lockouts, encrypted SSH secrets, audit
  log, no logging of the resources users visit.
- **Operations**: every server change is a background operation with a live log under a server
  lock; backups of the panel and of the server files; releases with ready images, `install.sh`
  and automatic updates from GitHub. Development goes on the `dev` branch, `releases` holds only
  releases, and `install.sh` takes the latest one.
- **Interface** in Russian and English, light, dark and system theme, adaptive layouts.
