# Server groups and relay

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
  before the join, or a clean AmneziaWG with a new key if it had no clients. Before that the panel
  keeps the group files it ran as a snapshot "before taking a joining server out of its group"
  ([Backup](Backup.md#amneziawg-snapshots-around-a-group)). A server that runs a key of neither is refused untouched.
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
group, revoked ones are gone, and it is back in the relay list. While the catch-up runs, the
member list links to its operation, and the `member_lagging` notification names the reason of the
lag too. When a lagging member already has
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
mobile networks with losses, so the setting is off by default. It restarts the protocol on every
member, but clients keep their old keys: each side runs its own timers, so a profile with the old
ones still connects, and the form asks for no confirmation. A key imported after the change gets the
new timers. Turning the setting off brings back `5-15` and `3-7`; while the checkbox is left as it is, the two
fields keep what was typed in them.

**Plans** give a group as a whole: the **Server groups** field of a [plan](Users-plans-limits.md#plan)
lets its users get the key of the group, which works on each member whoever leads the group. The
portal, the bots and the admin interface name such a key by the group. Servers of a group are not
offered among the plan servers: a plan that named a server before it joined a group gets the group
instead, and when a group is deleted its plans get its last leading server back.

## Relay

Clients of a group connect to the relay, which forwards their traffic to a live member. Any added
server can be the relay, including the panel's own machine.

1. In the group settings, pick the server in the **Relay** field.
2. On that server's card, open **Relay** and press **Install**. A background operation checks the
   OS, adds the service peer "Lamnezia relay" to every member, builds the `lamnezia-relay`
   container and publishes the ports of the groups as UDP.

The service peer goes to the leading server first. If the leading server is busy with another
operation or fails to take the peer, the install or update fails and nobody is marked lagging: the
relay keeps the members it has and the next sync tries again. A leading server that does not answer
is left for the next sync too, without lagging. Only another member that fails is marked lagging and
gets the peer on its catch-up.

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
through the service peer and sends clients to the first live one. Each probe goes from a new random
port: a filter on the way that lets a known flow through and drops new ones does not hide a member
the clients cannot reach. A switch takes the first member
that answered in the same round, the `nftables` rules change in one transaction, and old
connections are dropped. The
agent does not need the panel: switching works even while the panel is down. Its choice lives in
the `lamnezia-relay-data` volume, so neither a restart nor a new container after a port change
moves clients off the active member. The address of the service peer is never given to a key,
even on a member that does not have the peer yet.

To service the active server, press **Switch to this server** next to another member on the
group page. The panel asks the agent to send the clients there; the agent probes that member once
more and switches the same way as on a failure, or refuses and keeps the active one when the
member does not answer. Every client of the group loses its connection for 10-15 seconds with the
default timers ([measurements](#measured-switching-times)). The
switch is a background operation; it ends with the notification of a switch. A relay agent of an
older release does not answer the request: update the relay on the server card first. The agent
then goes on switching by itself as before: a planned switch does not pin the group to the member.
Under the member list the group page shows the last planned switch: when it was started, its
status with a link to the operation and, if it failed, the reason.

When the members, their order, the lagging ones, the port of a group, the address of a member
server or the keys and parameters of a member change, the panel updates the relay by itself right
after the change: only the relay of that group (and the old one when a group moves to another
relay), once for all the changes saved together. Every `LAMNEZIA_RELAY_SYNC_INTERVAL` seconds it also checks what no change in the
panel shows: a member name in DNS that now points to another IP address updates the relay too.
**Update** on the card does it at once. A relay the update could not help is left as it is, and
the card says why: a member of a group runs on the relay server itself, or the name of a member
server does not resolve.

The card also sets how often the agent probes each member (5 seconds by default) and after how
many failed probes in a row it moves a group to the next member (3 by default). **Save the
settings** writes them to the relay by a background update, without a reinstall; a shorter
interval or fewer failures switch sooner but react to short losses too. **Remove** drops the container, the image and the volume, then takes the service peers off
the members, the leading server first. A member that does not answer is marked lagging and loses
the peer on its catch-up; when the leading server does not answer, the panel keeps the peer on every
member, and when it fails to drop the peer, the removal fails. Keys of the groups go back to the
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

The agent also watches whether the active member answers the clients themselves. When several
different clients send to it for a few rounds in a row and none of them gets an answer, with no
answered client either, the group page marks the member **clients get no answer** and the relay card
notes it at the group. The agent does not switch on it: no member answers a key deleted from the
server. Move the group to another server with **Switch to this server**. The mark appears once the
relay is updated (**Update** on the card).

On every reading the panel compares the groups and members the agent forwards with what the panel
wrote. When the config on the server was replaced (a group the panel does not have, a group of the
panel missing, or other members in a group), the relay card warns that **the relay on the server
has other settings** and lists the differences; foreign groups are left out of the traffic list.
The next sync writes the panel's settings again by itself and, when a port of a group is not
published, starts the container again with the right ports. If the same difference stays after
that, the panel no longer updates the relay by itself and only shows the warning: press
**Update** or **Reinstall**.

### Split routing

The relay agent can also run a group in **split** routing instead of forwarding its port. The relay
then answers the clients itself with a copy of the group's interface (same key and parameters, the
group's port), sends traffic to the networks of the group's list and to private networks straight
out of the relay, and sends everything else to the active member over an AmneziaWG uplink with the
group's parameters. A switch to another member only changes the uplink's address: the clients'
sessions stay. When the uplink fails on every member while a member still answers the usual probe,
the group falls back to plain forwarding to that member; after a few good uplink checks in a row
(6 by default) it goes back to split. When no member answers at all, the group stays split, so the
sites of the list keep working. A network list that is empty or has a bad line is not applied; the
group keeps its previous list. The mode the group is in is part of the state the agent writes.

The group page shows the routing of a group behind an installed relay and switches it:
**Switch to split routing** and **Switch to forwarding**. The switch is a background operation:

1. The panel reads the traffic counters of the members, so the traffic of the old path is counted
   up to the switch.
2. For split routing, every member gets a second service peer, "Lamnezia uplink", by the usual
   protocol of changes: the leading server first. If the leading server does not answer, the
   operation fails and nothing changes. A member that does not answer falls behind and gets the
   peer on its catch-up.
3. The panel saves the routing and updates the relay: it writes the clone (the key, address and
   parameters of the group's leading server) and the peers of the group's keys, then the config.
   Each client makes one new handshake.

When the group goes back to forwarding, the relay stops using the clone first, and only then the
uplink peer is taken off the members. While a group is split, issuing, revoking and editing its
keys reaches the clone too: the relay gets the new peers within seconds, and the sync by the minute
catches what was missed. The clone has the keys the panel knows; peers added on the server by hand
are not copied. The switch is refused while another operation on the relay runs. The agent needs
the new relay image: use **Reinstall** on the relay card once.

#### Network list

A split group shows its **Network list** on the group page. The relay sends the networks of the
list straight out and everything else abroad over the uplink (a "black list" of what stays at home).

- **Sources.** On the first switch to split routing the group gets one source: the RIPE NCC
  delegations to Russia. Add more by address and format: networks one per line (`#` starts a
  comment), the site list of the Amnezia application (JSON), or the RIPE NCC delegations. A source
  can be turned off or deleted. The panel downloads every enabled source once a day (each address
  once for all groups), merges the networks of all sources and gives the relay the result as a
  file. The relay itself never downloads lists.
- **Protection.** A list that did not download, did not read or has less than half of the networks
  it had before is not taken: the group keeps its list, the source shows the error, and the panel
  tries again every hour. While a source fails, the administrators get the notification
  **Network list not updated** once a day, with the number of days since the last good list.
- **Own entries.** An IPv4 network, an address or a domain that always goes one way:
  **straight from the relay** or **abroad through the uplink**. An entry abroad wins over the lists.
  Domains are looked up by the relay itself, every 10 minutes and right after a change; a name that
  does not resolve keeps its last addresses. The panel resolves no domains, and the relay logs
  neither the names nor their addresses. A site behind a CDN may share addresses with other sites:
  they then go the same way.

A list or entry change reaches the relay within seconds; the relay applies the networks and
addresses in one nftables transaction.

#### DNS

In split routing the relay answers the DNS queries of the group's clients itself. A query (UDP or
TCP, port 53) from a client to the DNS addresses of the group's keys goes to a resolver in the
relay container, which asks Cloudflare (1.1.1.1, 1.0.0.1) over DNS-over-TLS straight from the
relay. The answers are those for the relay's country, and the network between sees no names: only
DNS-over-TLS leaves the relay. The resolver writes no log of queries or answers, not even to the
container output, and answers no one but the clients of split groups. Keys do not change.

The resolver runs only while some group is split. In forwarding, and while a split group has
fallen back to forwarding, the queries are not intercepted and go to the servers as before. A
resolver that does not start or stops is started again after a minute; until then the queries go
where the keys send them. Only the keys' DNS addresses are intercepted: a DNS server set by hand on
a device is not. The resolver comes with the new relay image: use **Reinstall** on the relay card
once.

## Keys of a group behind the relay

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
visit after the check. Until then the page says the domain is being checked.

Traffic, online status and limits of a group key are summed over all members: whichever server the
relay sends the client to, its traffic counts for the same key.

## Measured switching times

Measured on a group of two members behind a relay, with the default settings of the relay (a probe
every 5 seconds, a switch after 3 failed probes in a row) and of the keys (`KeepaliveTimeout = 5-15`,
`RekeyTimeout = 3-7`). Two clients pinged through the tunnel every 0.2 seconds.

| What happens | The relay switches after | Clients without connection |
|---|---|---|
| The active member stops answering (its UDP port is blocked) | 12-14 s | 17-22 s |
| Planned switch (**Switch to this server**) | 5 s | 11-14 s |
| The active member leaves the group | 8 s | 19-21 s |
| A member that is not active stops answering | no switch | none |

After the switch a client needs a new handshake with the new member; its timers decide when it
tries, so the **Short timers** above cut that part to a few seconds. A member that
did not answer during a key change was marked lagging with the reason "unreachable"; once it
answered again, one catch-up gave it the missed key. A key revoked afterwards was gone from both
members after one operation.
