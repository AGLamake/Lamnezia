# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Notification that a key works**: the owner of a new key hears once (`key_connected`) when the
  first traffic reading after the issue sees it in use — a handshake or counters that grew, on any
  member of a server group. A key reissued for the same device does not tell again; imported keys
  do not tell. Administrators get it only when they route it to a channel. **Upgrade**: a
  migration; keys issued before it count as connected and do not tell.
- **Restart of a WireGuard-family container that lost its interface**: the monitor reading now
  checks the VPN interface inside running AmneziaWG, legacy AmneziaWG and WireGuard containers.
  After 3 readings in a row without it, the panel restarts the container with a background
  operation under the server lock, at most 3 times with a pause of 5, 10 and 20 minutes, and then
  only notifies. New notification events `interface_lost`, `interface_restored` and
  `interface_repair_failed`; the server card shows the missing interface and the restarts. Nothing
  is restarted while an operation on the server runs, and a container stopped by the
  administrator is left alone. **Upgrade**: a migration.
- **VPN clients cannot reach the cloud metadata service**: a rule in the `DOCKER-USER` chain of the
  server drops traffic from the Docker bridges of the Amnezia containers to `169.254.0.0/16`, where
  clouds serve the server's data and the tokens of its service account; the host itself still
  reaches it, and the containers do not change. A systemd timer puts the rule back after reboots.
  An install closes the server on its own, an import starts a separate operation for it; the server
  card shows the state and can open the service for a hoster whose local services clients need.
  **Upgrade**: a migration; servers already in the panel are closed by their next install or
  import, or by *Close* on the card.
- **Several moderators per user**: a user may be served by several moderators (Django admin,
  **Staff members of the user**) and is then in the zone of each; all of them see the user's
  requests, and a request can be assigned to any of them or to an administrator. The user quota
  of each moderator still limits linking users by hand and by invite; a key of a shared user
  counts for the key quota of each moderator. A plan can name its staff members too (`Plan.staff`),
  for the plan-based serving that comes later. The API adds `moderator_ids` to users;
  `moderator_id` stays and gives the first moderator. **Upgrade from 1.0.0**: migrations only —
  each user's moderator becomes their single staff member, and the users, servers, quotas and
  requests of every moderator stay the same.
- **Screens and notifications of split routing**: the group page and the relay card show the mode
  the agent keeps a split group in (split routing or emergency forwarding, and since when), the
  active exit and whether the uplink works; the relay card also shows the CPU load of the relay
  server, and the network list tells how many networks the relay applies and when a list was last
  taken. New notifications `split_emergency` and `split_restored` come when the agent moves a
  split group to forwarding and back by itself. The wiki describes split routing, the server
  requirements and its limitations.
- **Traffic and limits of split routing from the clone**: in split routing the panel reads the
  traffic and online status of the group's clients from the clone on the relay and counts them
  for the keys of the leading server; the uplink peer on the members counts for the exit load of
  the group (`uplink:<group>`), not for the keys, so nothing is counted twice. A switch of routing
  reads the clone too; keys count on the clone from zero, and its counters are left at zero when
  the group is forwarded again. A key turned off by its traffic limit leaves the clone within
  seconds, and the speed limits of the leading server's keys go onto the clone as `tc` rules. The
  relay needs **Reinstall** once for the new agent.
- **DNS resolver of split routing**: in split routing the relay intercepts the DNS queries of the
  group's clients to the keys' DNS addresses and answers them with its own resolver, which asks
  1.1.1.1 over DNS-over-TLS straight from the relay. The resolver logs no queries or answers and
  runs only while some group is split; forwarding intercepts nothing. Keys do not change; the
  relay needs **Reinstall** once for the new image.
- **Network list of split routing**: the group page of a split group shows its network list. A
  group gets the RIPE NCC delegations to Russia as its first source; more sources are added by
  address in one of three formats (networks one per line, the site list of the Amnezia
  application, RIPE NCC delegations), and turned off or deleted. The panel downloads every source
  once a day and gives the relay the merged list; the relay never downloads lists. A list that did
  not download, did not read or has less than half of the networks before is not taken, and the
  administrators get `split_list_stale` once a day. Own entries — IPv4 networks or domains, sent
  straight from the relay or abroad through the uplink — win over the lists; the relay agent looks
  up the domains every 10 minutes and logs no names. A missing list file on the relay now means
  an empty list.
- **Signed release provenance**: a release publishes signed build provenance attestations for the
  image and for its files, and a signed SBOM of the image. A release also carries the code of the
  tag as `lamnezia-X.Y.Z.tar.gz`. `gh attestation verify` checks a downloaded image or file.
- **Another server address for a key**: a server takes other IPs or domains, and a background
  check compares the SSH key shown at each with the server's. A key can send its client to a
  checked address: it goes to the `Endpoint` of the config and the `hostName` of `vpn://`, the
  peer on the server stays, and the owner is told to import the key again. A reissued key keeps
  the address; keys of a server group are not offered other addresses. Removing an address moves
  its keys back to the server's own one.
- **Speed limit of the server**: the settings of an AmneziaWG or WireGuard protocol take a total
  download and upload limit. Inside it the speed is shared equally between those who download
  now, and the speed limits of plans work inside it. The limit is shown on the server card; in a
  group it is set on the leading server.
- **Split routing of a group**: the group page switches a group behind an installed relay between
  forwarding and split routing, as a background operation. It reads the traffic counters of the
  members first, gives every member a second service peer for the uplink by the protocol of
  changes (members that do not answer catch up later) and writes the clone of the leading server
  and the peers of the group's keys to the relay; each client makes one new handshake. Issuing,
  revoking and editing keys of a split group reach the clone too. Going back to forwarding takes
  the uplink peer off the members after the relay stops using it.
- **Split routing in the relay agent**: a group of the relay can run split instead of forwarding
  its port. The relay answers the clients with a copy of the group's interface, sends traffic to
  the networks of the group's list and to private networks out directly and the rest to the
  active member over an AmneziaWG uplink; a switch of the member only changes the uplink's
  address. When the uplink fails while a member answers, the group falls back to forwarding and
  returns after good uplink checks in a row (`recovery`, 6 by default). The config and state
  formats carry the routing, the clone, the uplink and the mode; an empty or broken network list
  is not applied. The relay image gets `iproute2` and a loose reverse path check. Panel settings
  come later.
- **I1 masking templates for AmneziaWG**: instead of the same packet of the Amnezia app on every
  server, I1 is built from a template: STUN (WebRTC), QUIC Initial, a DNS query with EDNS0, SIP
  INVITE or a DNS answer for icloud.com with a real Apple address. Per-server fields differ, and
  only tags the kernel module also knows are used. The template is chosen on install (random by
  default), on the move to 3.1 (the app's I1 is replaced, an own one stays) and in **Settings**,
  which reminds to replace the app's I1; for a group it is a group setting.
- **Install without questions**: without a terminal (cloud-init) or with `LAMNEZIA_UNATTENDED=1`
  `install.sh` asks nothing and takes its options from `LAMNEZIA_DOMAIN`, `LAMNEZIA_EMAIL`,
  `LAMNEZIA_PROXY`, `LAMNEZIA_IMAGE`, `LAMNEZIA_DIR` and `LAMNEZIA_REF`. A new installation then
  also gets its first administrator with a generated password; the panel address and the password
  go to `install-summary.txt` (mode 0600) in the installation directory, not to the output. An
  example of cloud-init user data is in `deploy/cloud-init/`. With a terminal nothing changes.
- **Device limit for the subscription link**: the panel remembers the devices that fetch the
  subscription link with a device ID (`x-hwid`, sent by Happ and others) and shows them on the
  portal and the user card, where each can be forgotten. A plan's **Subscription devices at most**
  refuses a new device over the limit with `403` and an explanation; empty means no limit, as
  before. **Settings → Subscription without a device ID** decides whether clients without the ID
  get the link (yes by default). Only a hash of the ID is kept; the XRay server is not touched.
- **Subscription link for Clash/Mihomo and sing-box**: the XRay subscription link gives
  Clash/Mihomo clients a Mihomo profile in YAML and sing-box clients a sing-box profile in JSON,
  chosen by the client's `User-Agent` or by `?format=clash|singbox|raw`. Other clients get the
  `vless://` links as before. The response also carries the portal keys page
  (`profile-web-page-url`) and support (`support-url`).
- **Telegram forum topics for notifications**: a Telegram channel takes `chat_id:topic_id`, e.g.
  `-1001234567890:42`, and posts to that topic of a supergroup, so servers and support can go to
  different topics. A plain chat ID works as before and posts to General.
- **Proxy for outgoing requests**: where Telegram or GitHub is blocked, `LAMNEZIA_TELEGRAM_PROXY`
  sends the bot and the Telegram notifications through an `http://` or `socks5://` proxy, and
  `LAMNEZIA_TELEGRAM_API_SERVER` points them to a self-hosted Bot API server;
  `LAMNEZIA_GITHUB_PROXY` does the same for the Amnezia release links and the panel update check.
  Without them nothing changes. The VPN servers and webhooks are reached directly as before, and the
  proxy password is never logged.
- **API for integrations**: billing, bots and monitoring read the panel at `/api/v1/` with a
  token: servers and their state, users with plans, limits and usage, keys without key material,
  and traffic by hour or day and per key. An administrator creates tokens in **System → API
  tokens**; a token acts as an administrator or a moderator and sees what they see, the value is
  shown once and only its hash is kept, a token can have an expiry and is disabled for good.
  Every call goes to the audit log, the rate is limited by `LAMNEZIA_API_RATE_LIMIT` (120 a
  minute), and the description is OpenAPI 3.1 at `/api/v1/openapi.json`. Version 1 only reads.
- **Report to the staff**: in **Settings → Report to the staff** an administrator turns on a
  daily or weekly summary (off by default): servers and relays down, traffic of the period and
  the most active keys, access ending within a week, failed operations, the age of backups and
  of the panel archive, applications and new support requests. It is built from what the panel
  already knows, with no request to the servers. The channels subscribed to `staff_report` get
  the whole panel, each moderator gets their own zone (`zone_report`) by email or in their
  messenger. **Send now** sends it at once for a check.
- **Sign-in alerts**: a sign-in from a network or browser the account has not used before is
  reported to its owner (`sign_in_new_place`) with the browser, the address and a link to the
  sessions of the profile. Administrators and moderators always get it, a portal user turns it on
  in **Profile → Notifications**. The panel keeps only a keyed hash of the network (/24 or /64)
  and the browser name to compare with. A lockout after wrong passwords is reported to the
  channels subscribed to `sign_in_locked`, once per lockout, with the name and the address and
  never the password.
- **Server load alerts**: the monitor tells administrators when the CPU, memory or disk of a
  server stays at or above a threshold (`server_load_high`) and when it is back to normal
  (`server_load_normal`). **Settings → Server load** sets the thresholds (90% each by default,
  0 turns one off), how many readings in a row count (3) and how often a lasting high load is
  repeated (6 hours), so a short spike or a load swinging around the threshold wakes nobody.
  The server card and **Needs attention** show the high load too.
- **Notification about dropped AmneziaWG 3.1 settings**: when the amneziawg kernel module on a
  server is older than 3.1 and amnezia-awg2 runs without the 3.1 settings, administrators get one
  `awg3_dropped` notification telling them to update or remove the module on the host and restart
  amnezia-awg2, and one `awg3_restored` when the settings are back. Both are ticked per channel like
  the other monitor events.
- **Traffic runs out soon**: a user with a traffic limit hears once per period of that limit that
  they have used a share of it (`traffic_low`, 80% by default, **Settings → Traffic warning**,
  0 turns it off), before the keys go off. The plan, the user's own limits and the traffic of group
  servers count; a new period, another plan or a changed limit warns again. When the traffic of
  some keys is not counted now, the message says the real share may be higher. It links to the
  usage page of the portal and can be routed to channels like the other events.
- **Hosting and payment link of a server**: the server form takes an optional link to the hosting
  provider or its billing page (`http://` and `https://` only). The server card shows it under
  *VPS paid until* and opens it in a new tab, and the VPS payment reminder on the dashboard has a
  button with the same link.
- **HTML notification letters**: notification letters to users and to email channels now carry an
  HTML version in the same frame as the account letters — the event's title, its text and the link
  to its page as an **Open in the panel** button; the plain text stays as it was. The offer to link
  Telegram or Discord in the letters about the end of access and the limit is a block of its own
  with a **Link** button to the profile.
- **Panel update from the interface**: twice a day the panel asks GitHub for the latest release
  (only its tag, nothing is sent) and shows administrators the running and the new version with a
  link to the CHANGELOG; `LAMNEZIA_UPDATE_CHECK=False` turns the check off. On **System → Panel
  update** an administrator updates the panel: it makes a panel archive and leaves a request that the
  new `lamnezia-update` unit on the host picks up. The host takes only a newer release tag of the
  installation's branch, checks it against the manifest published with the release, waits for
  `/healthz` and goes back to the previous version when the panel does not come up. The panel gets
  no Docker or SSH access to the host. Existing installations get the unit by running `install.sh`
  of this version once; until then the page only tells about the new release.
- **Messages show before the answer**: a message in the support chat, a request or the inbox
  appears in the thread at once, muted and marked *Sending…*, and the field is cleared for the next
  one; a screenshot shows after the upload. A message that could not be sent stays marked
  **Not sent** with a **Retry** button and its text kept; a retry is never saved twice.
- **Telegram straight from the offer card**: **Link Telegram** on the card that offers to link a
  messenger opens the bot by its `t.me` link with a link code in a new tab, without the profile;
  the card waits for the confirmation in the bot and goes away once the account is linked.
- **Speed test by port in the browser**: the speed test can now run right on its page. The
  temporary container serves WebTransport (HTTP/3 over UDP) with its own short-lived certificate,
  and the page, opened on a device in the network to check, measures download and upload to every
  port with no `iperf3` on the device. Needs Chrome, Edge or Firefox and a server with an IPv4
  address or a domain; *iperf3 on a device* stays as the other way to measure.
- **Notifications in the browser (Web Push)**: in **Profile → Notifications** a user turns on
  notifications in this browser or the installed app (Android; iPhone and iPad from iOS 16.4 when
  added to the Home Screen) and gets there the same events as by email or messenger; a click opens
  the page they are about. Administrators get routed events through the new **Browser push**
  channel. The panel makes its own VAPID keys and keeps the private one encrypted; pushes go only
  from the background worker, and subscriptions the push service reports as gone are deleted.
  Signing out turns notifications off in that browser.
- **Messenger offer in letters**: the letters about the end of access and the limit tell a user
  without linked messengers that notifications can come to Telegram or Discord, with a link to the
  profile, when the administrator has set up a bot.
- **Pictures sent as files in Telegram**: an image sent to the bot as a file, not as a photo, goes to
  support like a photo does.
- **Who took an incident**: an incident remembers the staff member who took it, and its card in the
  bot names them even after its requests are closed.
- **Staff commands in the Telegram menu**: a staff member's own chat with the bot lists
  `/admin`, `/overview`, `/today` and `/incidents`; users do not see them.
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

- **Upload speed limits are shaped, not policed**: upload goes through an IFB device with the same
  HTB classes as download, so packets over the limit wait instead of being dropped. The operation
  loads the `ifb` module on the host and keeps it loaded after a reboot; without the module upload
  is policed as before and the server card says so. With no limits at all `tc` is not installed.
- **A change of I1–I5 alone** asks for no confirmation and no longer restarts AmneziaWG: the server
  does not read these packets, so only the config and the key profiles are updated and old profiles
  keep working. I1–I5 with `<c>` (unknown to the kernel module) or larger than 1400 bytes are refused.

- **Image built from tracked files only**: `.dockerignore` is a whitelist of what git tracks, so a
  local build of a working copy no longer takes in its untracked files, notes or scripts; compiled
  translations and caches in subfolders stay out too. The CI and release builds are unchanged.
- **Tests in a random order on all cores**: pytest-randomly and pytest-xdist are development
  dependencies; `make test` and CI run `pytest -n auto`.
- **WebSockets closed during a panel move**: while the panel is read-only, a new WebSocket is
  refused and a message on one opened before closes it unread, so no consumer can change data
  behind the move; the support pages fall back to polling. `/ws/ping` keeps answering.
- **No useless reminder**: an answer reminder is not created when the only channel the user has
  left is the one the answer already reached.
- **Big pictures not downloaded**: a picture over 5 MB sent to a bot is refused by its size before
  it is downloaded, in Telegram and in Discord.
- **No Plans link for moderators**: the admin menu of a moderator no longer links the plans page,
  which only administrators may open.
- **Subscription address in the portal app**: the `/sub/<token>` handler moved from `apps/keys`
  to `apps/portal`; the address stays the same.
- **Service copies are files**: the copy of a service (the Tor site, SFTP uploads, proxies) is now
  an encrypted file in the `media` volume under a key of its own, not a value in the database. The
  files are fingerprinted on the server first: unchanged since the last copy, they are not sent
  again, and the backup section says when they were checked. Files larger than the limit are not
  copied, the last copy stays, and the backup section shows a warning with their size. Copies made
  by an older panel stay readable until the next one replaces them.
- **The Tor site copy holds the WordPress core**: the whole `/var/www/html` is copied, so a restore
  brings back the core version the site ran, not the one of the image.
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

- **Relay stopped forwarding after the split routing update**: the agent named an nft chain
  `mark`, which is an nft keyword, so the whole rule set failed to load and no group was
  forwarded. The chain is now called `steer`; updating the agent brings the relay back.
- **"Unreachable from the relay" every few minutes**: a group member that answered the relay
  only now and then was told unreachable after each run of failed probes and reachable after each
  answer. Now `member_unreachable` comes once per outage, and the outage ends with
  `member_reachable` only after the member has answered every probe seen for 5 minutes.
- **A panel move stuck after a partial confirmation**: when the new machine went down in the middle
  of the confirmation, the cancel refused and only a retry was left. The move screen now names the
  state and offers **Retry the confirmation** and **Cancel without the new machine**: the latter
  lets this panel accept changes again without touching the new machine and warns that the copy
  there may run as a second panel until it is removed by hand.
- **A relay member that answers the relay but not the clients**: the agent probed a member through
  one long-lived flow, so a filter on the way that dropped new UDP flows let the probe through while
  the clients got nothing, and the panel showed the member as reachable and active. Each probe now
  goes from a new random port. The agent also counts clients that send to the active member without
  an answer: when several do for a few rounds in a row, the group page marks the member **clients get
  no answer** and the relay card notes it. The agent does not switch on it. Press **Update** on the
  relay card to get the new agent.
- **A relay with replaced settings is noticed and repaired**: the panel compared a relay only with
  what it had written itself, so a relay whose config.json someone replaced (another group, other
  members) went unnoticed, and its card showed the foreign group with an empty server list. Every
  reading now compares the groups and members on the relay with the panel's; a difference shows on
  the relay card, and the next sync writes the relay again, starting the container again when a
  port of a group is not published.
- **Title of a "server does not work" request with a topic**: such a request showed as
  "Server Helsinki is slow does not work"; it now shows its topic, the server separately, and keeps
  "Server … does not work" only for the quick report without a topic.
- **`git pull` after running the installer again**: on an existing installation `install.sh` left
  the code on no branch, so `git pull` from the upgrade guide failed. It now keeps a local branch
  tracking `origin/<ref>` (`releases` by default); a tag in `LAMNEZIA_REF` still installs that fixed
  version.
- **Demo data ready for screenshots**: after `seed_demo` the portal shows traffic for the period
  (the plans now start a few days back), the demo chat has a support reply, and tickets and chat
  messages are written in the panel language.
- **The limits turned on the keys of a blocked user**: in a new period or after an extension the
  scheduled limit run turned back on the keys of a user blocked meanwhile; they now stay off.
- **Moderator key quota race**: two keys issued at once from the admin interface could both pass a
  moderator's key quota; the quota is now checked again with the moderator's row locked.
- **A restore cut off right after the container was created was refused**: the container is now
  labelled at `docker run`, so a restore run again removes it even when the worker stopped before
  the files went in.
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
- **Secrets of operations, one filter**: every operation that is given a secret (the SOCKS5
  password, the AmneziaWG HeaderProtectionKey, the panel archive password) keeps it out of the
  audit log, the step log and the operation page, also in its encrypted form; the parameters reach
  the audit log through one filter only.

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
