# Users, plans and limits

A plan is a tariff and a group at once: it says which servers and protocols its users get and
what limits apply. Plans are managed in **Plans** in the admin interface (`/plans/`, administrators).

## Plans

### Plan

| Field | Meaning |
|---|---|
| Traffic per period | GB a user may use per period; the period length is in days (30 by default). Empty: no limit. |
| Traffic in total | GB over the whole access term. Empty: no limit. |
| Keys at most | How many keys a user may have. Empty: no limit. |
| Speed, Mbit/s | Speed of every key of a user, download and upload each. Empty: no limit. See [speed limits](#speed-limits). |
| Access, days | Term from the day the plan is assigned. Empty: no end date. |
| Servers, protocols | Where users get keys. None selected: all. |
| Trial | Marks a trial plan. |
| Price, currency | With a [billing plugin](Billing-plugins.md), a plan with both and not a trial is sold in the portal. Without plugins, information only. |

1 GB here is 1024³ bytes. A plan that users still have cannot be deleted: give them another one first.
A plan that was paid for stays for the record: clear its price to stop selling it.

## Plan of a user

On the **Plans** page enter a user name under **Plan of a user**. The page shows the limits that
apply to the user now and lets you change the plan and personal limits:

- a personal limit replaces the plan value for this user only; empty takes the plan value;
- **Access until** replaces the end date the plan gives;
- a new plan starts the access term again; personal limits are kept.

A user without a plan cannot get keys. Changes of plans and limits are written to the
[audit log](Audit.md).

## Applying limits

Every `LAMNEZIA_TRAFFIC_INTERVAL` seconds (5 minutes by default) the panel checks users with a
plan and turns off all their active keys — status "disabled by limit", the peer is removed from
the server, its address and keys stay in the panel — when:

- the access term is over;
- traffic since the plan was assigned has reached "Traffic in total";
- traffic in the current period has reached "Traffic per period".

Traffic counts both directions of every key of the user, revoked keys too. Periods follow each
other from the day the plan was assigned; traffic is counted by the hour, so the hour a period
starts in belongs to the period before.

Keys disabled by limit come back on by themselves once the user is within the limits again: a
new period starts, access is extended ("Access until"), a plan is assigned or a limit is raised.
Keys an administrator disabled by hand are never turned on by the panel. A key enabled by hand
for a user who is still over a limit is turned off again on the next check.

Users without a plan and keys without an owner are not checked. Keys are turned off and on by
background operations (shown in the operation journal) and written to the [audit log](Audit.md)
without an author.

## Speed limits

The speed of a key is limited on the server, inside the protocol container, by its address: `tc`
shapes download on the interface (HTB) and drops upload over the limit (policing). Each key of the
user gets the full speed; the limit is not shared between the keys. Users without a speed limit
and keys without an owner are not limited. AmneziaWG, WireGuard and XRay (see
[XRay](#xray-speed-limits) below). Policing passes packets
up to 64 KB, which the tunnel glues from smaller ones, and lets a tenth of a second at the limit
through at once (at least 128 KB), so a limit does not cut upload far below its value.

A limit is applied and removed without a restart of the container and without breaking the
sessions of other clients:

- at once when the speed of a plan or a user, or the plan of a user, is saved;
- otherwise every `LAMNEZIA_TRAFFIC_INTERVAL` seconds, which also picks up issued, enabled and
  disabled keys and bulk plan changes.

Only a server whose rules changed is touched, by the **Apply speed limits** operation in the
operation journal. A failed operation is not repeated by itself: run it again from the journal, or
the next change starts a new one.

On the server the panel keeps the rules in `lamnezia_speed.sh` next to the protocol config
(`/opt/amnezia/awg/` for AmneziaWG) and adds one line before `tail -f /dev/null` in
`/opt/amnezia/start.sh`, which runs it after the interface comes up, so the limits stay after a
restart of the container or the server. If the container image has no full `tc`, the operation
installs `iproute2-tc` from Alpine packages (the container needs internet access once).

Reinstalling the protocol in the Amnezia app recreates the container without these files: the
limits come back after the next change of limits or keys, or when the last operation is run again.

### XRay speed limits

XRay has no per-client speed limit, and the address of a client is that of its connection, so
XRay keys are limited another way, in both directions, TCP and UDP alike:

- `server.json` gets a `freedom` outbound `lamnezia-<mark>` with a socket mark for each limited
  key and a `routing` rule that sends the key (by its `email`) there. Other keys keep the first,
  default outbound; routing rules already in the file are kept and decide first.
- `nftables` in the container drops the traffic of the mark over the limit: outgoing by the
  mark, replies by the connection mark copied from it. The rules are in
  `/opt/amnezia/xray/lamnezia_speed.nft` (the data volume of the container); the operation
  installs `nftables` from Alpine packages and adds one line right after `killall -KILL xray` in
  `/opt/amnezia/start.sh`, so the rules are loaded before xray starts.

A new speed only reloads the `nft` rules. When the set of limited keys changes, `server.json`
changes too and the container restarts: every XRay client of the server reconnects. The limits
are applied to the traffic between xray and the internet; the client gets the same speed.

When the Amnezia app recreates the container or rewrites `server.json`, the routes, the start
line and the statistics blocks are gone. The traffic collector then finds no XRay statistics,
and the next run of the speed limits applies them again, statistics included. The operation fails
if the xray statistics API does not answer after it, and is then not repeated by itself.

## Users

The **Users** page of the admin interface (`/users/`, administrators and moderators) lists everyone
registered in the panel with their plan, access term and number of active keys. A moderator sees and
changes only their own users ([moderator's zone](Security.md#moderators-zone)).

### Filters

| Filter | Shows |
|---|---|
| Name or email | Users whose name or email contains the text. |
| Plan | Users on the plan, or users without a plan. |
| Status | Active, waiting for approval or blocked. |
| Server | Users with a key (not revoked) on any protocol of the server. |

### User card

A click on a name opens the card:

- **Plan and limits** — the value in the plan, the personal value and the one in effect for every
  limit; **Change** opens the [plan of the user](#plan-of-a-user);
- **Traffic** — used in the current period and since the plan was assigned;
- **Keys** — all keys of the user with their status and protocol.

### Bulk actions

Tick users in the list, choose an action under **With the ticked users** and press **Apply**:

| Action | What happens |
|---|---|
| Extend access | Adds days to the access term; an expired term counts from today. Keys disabled by the term come back on. Users without a plan or with an endless term are skipped. |
| Disable keys | Turns off all active keys by hand: limits will not turn them back on. Users without active keys are skipped. |
| Change plan | Gives the plan and starts the access term again; personal limits stay. Keys are turned on or off by the new limits. Users already on the plan are skipped. |
| Reissue keys | Gives every key of the users a new one; the old keys stop working at once. One `keys.reissue` operation per protocol does it for all the users. Users without keys or who may not have keys are skipped. |

The action opens a report: for every user it shows whether they were changed or skipped and why,
and the key operations it started. Keys change on the servers in the background; the report
updates itself until every operation finishes. A failed operation can be opened and run again.
Every changed user is written to the [audit log](Audit.md) (`users.extend`, `users.disable`,
`users.change_plan`, `users.reissue`).
