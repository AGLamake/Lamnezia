# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Automatic stop of the old panel after a move**: once a confirmed move's domain points to the new
  machine and the copy there answers, the old read-only copy asks its host to stop it; the
  *stop the old copy* operation marks it in the journal, and the new `lamnezia-stop.timer` of
  `install.sh` runs `docker compose down`. The option is on by default and can be turned off in the
  move form or on the move progress screen. Older installs need `install.sh` run again for the timer.
- **Payment totals in the orders list**: above the administrator's orders the paid ones among those
  found are summed by currency, so a date filter gives the takings of the period.
- **Who created an invite**: the administrator's list of invites in **Registration** names the
  staff member who created each one.
- **`LAMNEZIA_ORDER_EXPIRES_HOURS`** (24): how long an order waits for payment before it is marked
  expired.
- **XRay shortId change notice**: when the panel finds a new Reality shortId on an XRay server, the
  owners of keys shown before get the new `short_id_changed` notification: the keys added to the
  app no longer connect and must be imported again.
- **Prepare all imported XRay keys**: a button on the XRay row of the server card builds the
  profiles of every imported key in one background operation.
- **Move progress screen**: starting, confirming or cancelling a panel move opens a screen with the
  steps of the move's operations; after the hand-over it shows the time since the confirmation and
  whether the A record of the panel domain points to the new machine. A background job checks the
  domain once a minute, also on the read-only old panel, until it has switched.
- **App versions for AmneziaWG 3.1 keys**: the portal key page and the bot key card (and the key
  itself in the bot) list the minimum Amnezia VPN and AmneziaWG versions per platform for a key of
  an AmneziaWG 3.1 server; other keys show no hint.
- **Panel move**: **System → Panel move** moves the panel to another machine added as a server. A
  background operation installs the same code version there, restores a panel archive with a
  one-time password and checks the copy; after the confirmation the copy accepts changes and the
  old panel stays read-only. Only DNS is switched by hand. From the start the panel is read-only:
  changes answer `503`, background jobs and bots pause. A relay on the new machine is checked
  before and after the install and started again if needed; without nginx, Caddy ports are checked
  first. A cancel removes the copy. `install.sh --moving` retries the nginx certificate every 10
  minutes until the domain points to the new machine.
- **Panel archive and restore**: **System → Panel archive** (or `manage.py panel_archive create`)
  makes one file encrypted with a password holding the database, `.env` with the secrets key and
  the uploaded files; `install.sh --restore FILE` installs a new server from it with the same
  servers, keys and SSH credentials. Archives are kept in the new `archives` volume, the newest
  `LAMNEZIA_PANEL_ARCHIVE_KEEP` (3) stay; downloads and removals are audited. The image now has
  the PostgreSQL 17 client and is based on Debian trixie.
- **AmneziaWG 3.1 diagnostics**: when `awg-quick up` refuses the config, the error says whether the
  image does not know a parameter (move the server to 3.1) or S1–S4 are below 12 next to
  `HeaderProtectionKey`; the refused line goes to the log with the parameter name only. After
  every start the panel compares `awg show awg0` with `awg0.conf`: a kernel module older than 3.1
  that dropped the 3.1 parameters fails the change, and the monitor marks the server card.
- **Old images are removed**: the move to AmneziaWG 3.1 and the rebuild of AmneziaDNS remove the
  image left without a name.
- **TSO/GSO workaround after the move from legacy AmneziaWG**: a server with the workaround on
  applies it to the new `amnezia-awg2` at once.
- **Wiki page about server groups and the relay**, moved out of *Servers and import*, with the
  measured switching times: 12-14 s to switch and 17-22 s without connection when the active
  member fails, 11-14 s for a planned switch, with the default timers.
- **Incomplete traffic in usage and the forecast**: when the traffic of some keys of a user is
  unknown, the portal **Usage** page warns that its numbers and forecast may be below the real ones,
  and the **Limit runs out** list of the statistics marks the user *incomplete data*.
- **Domain check under way**: until the background DNS check of a group domain has answered, the
  group page says the domain is being checked.
- **Snapshot before undoing a failed join**: **Take out of the group** first keeps the group files
  of the server as a snapshot "before taking a joining server out of its group", so the rollback
  can be undone. Restoring such a snapshot warns that the server gets the group key back.
- **Reason of a lag and the catch-up under way**: the `member_lagging` notification names why the
  member lags, and webhooks get it as `reason` (`unreachable`, `missed`, `drift`, `stayed`). The
  member list of a group links to a queued or running catch-up of a lagging member.
- **Last planned switch on the group page**: when it was started, its status with a link to the
  operation and the reason of a failure.
- **A failed group join can be retried or undone**: a member stuck joining is marked "join failed"
  and gets **Retry joining** and **Take out of the group**. Taking it out puts back the snapshot
  taken before the join, or a clean AmneziaWG if the server had no clients; a server that still
  runs its own key keeps its files. **Keep in the group** after a failed leave starts the catch-up
  at once. The menu counter of groups to look at counts failed joins.
- **Full-size load chart of a relay**: the relay card stacks the load from the clients and to them
  in bytes per second, with a **24 hours** / **7 days** switch. `bar_chart` takes `rate=True` for
  values in bytes per second.
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
  mobile networks clients make more handshakes. Old keys keep connecting, so a change of the timers
  alone, here or in the settings of a server, asks for no confirmation and tells no owner to import
  the key again.
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

- **No Plans link for moderators**: the admin menu of a moderator no longer links the plans page,
  which only administrators may open.
- **Subscription address in the portal app**: the `/sub/<token>` handler moved from `apps/keys`
  to `apps/portal`; the address stays the same.

- **DNS rebuild step name**: the operation now calls the step "Start the new container" instead of
  "Start the container on the new port".
- **Panel move refuses the panel's own machine**: the form rejects a server whose address is the
  panel domain, the address the panel is opened at, or an IP address these names point to. The
  preparation checks again over SSH, before any change: the address the panel connects from, and
  the IP of its domain, must not be an address of the machine or of its containers.
- **A relay sync after a change touches only the relays of the changed groups** (and the old relay
  of a group that moved), with one `relays.sync` task per transaction however many records it
  saves. `relays.sync` takes the relay servers to compare; the sync by the minute still compares
  every relay.
- **Group keys in metrics and statistics**: `lamnezia_peers` and `lamnezia_peers_online` count a key
  of a server group once, under the leading server, instead of once per member. A member the panel
  cannot read marks the traffic of the group keys as unknown and is listed in **Traffic is not
  counted on some servers**.
- **Group guards and a faster group page**: the protocol settings operation refuses a group member
  even when started past its card. A joining server takes the DNS of the group. The DNS check of
  the group domain runs in the background and is cached for 10 minutes, so the group page no longer
  waits for DNS. The shared handling of AmneziaWG files moved to `apps/protocols`, and backups no
  longer depend on the groups.
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

- **The limits turned on the keys of a blocked user**: in a new period or after an extension the
  scheduled limit run turned back on the keys of a user blocked meanwhile; they now stay off.
- **Moderator key quota race**: two keys issued at once from the admin interface could both pass a
  moderator's key quota; the quota is now checked again with the moderator's row locked.

- **Building the profiles of imported XRay keys failed at the end**: the profiles were saved, but
  the operation was marked failed.
- **A failed service peer made the leading server lag**: when a relay update met the leading server
  busy with another operation (for example a change of the group settings), the leader was marked
  lagging and the relay got a config without the group, so every client lost connection; a group of
  one member could never catch up. A failure on the leading server now fails the relay operation
  and the relay keeps its members; a leading server that does not answer is not marked lagging.
  Only another member that fails lags, as in key changes.
- **The relay did not switch right after a member came back**: before a switch the agent probed
  the candidate a second time within milliseconds, and the member dropped that handshake, so on a
  short link the clients stayed without connection as long as the active member was down. The
  switch now takes the member that answered in the same round of probes. An installed relay gets
  the fixed agent after **Update** on its server card.
- **Relay install or update broke on a busy member** ("log() is only allowed inside a step") when
  another operation held the lock of a member while the service peer was being placed. The member
  is now left lagging for its catch-up with the reason in a step of its own.
- **Outage of a deleted server or relay stayed remembered**: deleting a server or a relay now
  forgets its open `server_down` / `relay_down` trouble.
- **Relay update failed with "Unexpected error" after every step succeeded** when the
  leading server of a group had imported keys: such keys have no profile and now keep their
  address. Saving the relay state is now a step of its own, so an error there is shown on it.
- A key of a server group issued while its relay was being installed kept the address of the
  leading server; it now takes the address the group has when the key is saved.
- **Relay install or update with no member of a group answering** no longer fails with an
  internal error: the service peer of that group is left for the next sync, and the operation log
  says why.

### Security

- **Service secrets on request**: the SOCKS5 password and the `tg://`/`t.me` links of Telegram
  proxies are no longer in the server card page. **Show** asks the panel for them (`no-store`), and
  every look is written to the audit log (*Show service password*, *Show Telegram proxy links*),
  as for SFTP.
- **Webhooks and DNS rebinding**: webhook and Discord sends connect to the address the private
  network check passed, with the Host header and the TLS certificate checked by name, so a name
  that changes its address after the check gets no request. A name that does not resolve is tried
  again later.

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
