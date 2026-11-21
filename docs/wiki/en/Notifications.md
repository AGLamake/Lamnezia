# Notifications

**Admin → Notifications** (`/notifications/`, administrators only) sends panel events to email,
Telegram, Discord, administrators' browsers and your own webhooks.

## Channels

A channel is one place messages go to. There can be several channels of each kind.

| Kind | What to fill in |
|---|---|
| Email | One or more addresses separated by commas. Letters go through the panel's mail server ([Configuration](Configuration.md)): plain text with an HTML version in the panel's style, where the event's link is an **Open in the panel** button. |
| Telegram | Chat ID of a user, group or channel the bot can write to. In a supergroup with topics add the topic number after a colon, `-1001234567890:42`: it ends the topic link (**Copy link** → `t.me/c/1234567890/42`); without a number the message goes to General. Bot token: the channel's own, or empty to use the one from the panel settings. |
| Discord | Webhook URL of a Discord channel (**Channel settings → Integrations → Webhooks**). Messages come as an embed. |
| Discord direct message | The Discord user ID (developer mode → right click → Copy User ID). Sent by the panel Discord bot; the user must share a server with it. |
| Webhook | Your URL and a signing secret. The panel posts JSON (see below). |
| Browser push | Nothing but a name: messages go to the browsers of administrators who turned notifications on in their profile (see [In the browser](#in-the-browser)). |

Tokens, webhook URLs and secrets are stored encrypted and are not shown again: leave the field
empty to keep the saved value. A disabled channel receives nothing but the test message.

Webhook and Discord URLs must lead to a public address. An address in a private network
(`127.0.0.1`, `10.0.0.0/8`, `192.168.0.0/16`, `169.254.169.254` and the like, also behind a host
name) is refused when the channel is saved and checked again on every send; redirects are not
followed. A send connects to exactly the address that passed the check, so a name that changes its
address right after the check (DNS rebinding) cannot lead the request into a private network. A
name that does not resolve gets the message later, on a retry. For a receiver in your own network turn on **Settings → Integrations → Notifications to
private networks**.

**Send test** delivers a test message to the channel at once; the result is in the log.

## Events and routing

On the channel page tick the events the channel should receive. The channel list shows which
channels hear each event.

| Event | `event` | `data` |
|---|---|---|
| Server is down | `server_down` | `server`, `host`, `last_seen` |
| Server answers again | `server_up` | `server`, `host`, `last_seen` |
| AmneziaWG runs without the 3.1 settings | `awg3_dropped` | `server`, `host`, `since` |
| AmneziaWG runs with the 3.1 settings again | `awg3_restored` | `server`, `host`, `since` |
| Server load is high | `server_load_high` | `server`, `host`, `resource` (`cpu`, `memory`, `disk`), `percent`, `threshold`, `since` |
| Server load is back to normal | `server_load_normal` | `server`, `host`, `resource`, `percent`, `threshold`, `since` |
| VPN interface lost | `interface_lost` | `server`, `host`, `container`, `since`, `attempts` |
| VPN interface restored | `interface_restored` | `server`, `host`, `container`, `since`, `attempts` |
| VPN interface not restored | `interface_repair_failed` | `server`, `host`, `container`, `since`, `attempts` |
| Key connected | `key_connected` | `user`, `key`, `key_id`, `server`, `url` |
| Limit reached | `limit_reached` | `user`, `reason` (`traffic`, `total_traffic`, `expired`) |
| Access ends soon | `access_expiring` | `user`, `until` (date) |
| Traffic runs out soon | `traffic_low` | `user`, `percent`, `used`, `limit_bytes`, `limit`, `incomplete`, `note`, `url` |
| Keys must be imported again | `keys_changed` | `user`, `server` |
| Keys revoked | `keys_revoked` | `user`, `server` |
| Keys moved to AmneziaWG 3.1 | `keys_moved` | `user`, `server` |
| Keys updated for a server group | `keys_updated` | `user`, `server` |
| Keys reissued | `keys_reissued` | `user`, `server` |
| XRay keys must be imported again | `short_id_changed` | `user`, `server` |
| Registration request | `registration_pending` | `user`, `email` |
| Sign-in locked after wrong passwords | `sign_in_locked` | `user`, `address`, `failures`, `minutes` |
| New support ticket | `ticket_created` | `user`, `type`, `subject` |
| Chat message | `chat_message` | `user`, `text`, `count` |
| Incident opened | `incident_opened` | `server`, `reports` |
| Incident closed | `incident_closed` | `server` |
| New article suggestion | `suggestion_created` | `user`, `kind`, `title` |
| Payment received | `payment_received` | `user`, `plan`, `amount`, `currency`, `provider` |
| Relay switched | `relay_switched` | `group`, `relay`, `from`, `to` |
| Group server unreachable from the relay | `member_unreachable` | `group`, `relay`, `server` |
| No group server reachable | `group_unreachable` | `group`, `relay` |
| Relay does not answer | `relay_down` | `relay`, `host`, `last_seen` |
| Group server lags behind | `member_lagging` | `group`, `server`, `since`, `reason` |
| Group server reachable from the relay again | `member_reachable` | `group`, `relay`, `server` |
| Group server reachable again | `group_reachable` | `group`, `relay` |
| Relay answers again | `relay_up` | `relay`, `host`, `last_seen` |
| Group server caught up | `member_caught_up` | `group`, `server`, `since` |
| Panel archive failed | `panel_archive_failed` | `error` |
| Network list not updated | `split_list_stale` | `group`, `source`, `days`, `error` |
| Emergency forwarding | `split_emergency` | `group`, `relay`, `server` |
| Split routing again | `split_restored` | `group`, `relay` |
| Report to the staff | `staff_report` | `period`, `since`, `servers_down`, `relays_down`, `groups_down`, `traffic`, `top_keys`, `access_ending`, `failed_operations`, `backups_oldest`, `archive_at`, `applications`, `new_requests`, `url` |

`server_up` and the last four come once when the matching trouble is over (`server_down`,
`member_unreachable`, `group_unreachable`, `relay_down`, `member_lagging`). The panel remembers
the trouble apart from notifications, so the pair comes even when no channel is subscribed to the
trouble event. Deleting a server or a relay forgets its outage without a recovery notification,
and so does a server that leaves its group while it lags: it no longer has a group to catch up
with.

**Keys reissued** (`keys_reissued`) comes to the owner once per reissue of their keys on one
server — by the staff, in a batch or after a change of group or protocol settings: the old keys no
longer connect, and the new ones are imported again. A user who reissues their own key in the
personal account or the bot is not told.

`member_unreachable` comes once per outage of a member. The outage ends, and `member_reachable`
comes, only after the member has answered every probe the panel saw for 5 minutes, so a member
that answers the relay now and then does not repeat the pair every few minutes.

Notifications about the user go where they chose on their **Messengers** page: email by default,
or their linked Telegram or Discord ([Telegram and Discord](Messengers.md)), and also to the
browsers where they turned notifications on ([In the browser](#in-the-browser)).
Users themselves get an email about their reached limit, when most of their traffic limit is
used (see below) and three days before their access ends, if they have an address. When an incident is closed, everyone who reported it gets an email
that the server works again (`incident_resolved`; it goes only to the users, never to a channel).

**Traffic runs out soon** (`traffic_low`): a user with a traffic limit hears once per period
of that limit that they have used the share set in **Settings → Users and keys → Traffic
warning** (80% by default; 0 turns it off). The limit of the plan and the user's own limits
count, and the traffic of group servers too. Each limit warns on its own: per period and in
total. A new period, another plan or a changed limit lets the panel warn again; an extension
of the same plan keeps the period and does not. A reached limit is not warned of: the user
hears of `limit_reached`. When the traffic of some of the user's keys is not counted now (a
server does not answer), the message says that the real share may be higher. The message
links to the usage page of the portal and is written in the user's language.

**Key connected** (`key_connected`): the owner of a new key hears once that it works — at the
first traffic reading after the issue that sees the key in use: a handshake or, for protocols
without one, counters that grew. In a server group a connection to any member counts. A key
reissued for the same device does not tell again once an earlier key of that device has
connected; imported keys and keys issued before the update never tell. The message links to the
key in the portal. Administrators get it only when they route it to a channel (off by default).

A letter to a user, like one to an email channel, is plain text with an HTML version in the
panel's style: the event's title, its text and the link to its page as an **Open in the panel**
button. The letters about the end of access and the reached limit offer a user without a linked
messenger to get notifications in Telegram or Discord (when the panel has their bot set up): as a
line with the profile link in the text, and as a block of its own with a **Link** button in the
HTML. A letter is written in the recipient's language.

An answer of the support staff goes where the user last wrote from: from a bot, the answer itself
goes to that messenger, even when they have no email (`chat_answer`, `request_answer`); from the
site, it waits on the site. When the user neither answers nor reads it in the panel within 2
minutes, the channel they chose gets one reminder with a link to the thread and without the text
of the answer, only to the user: "there is a new message in your support chat" for the chat
(`support_answer`, data `user`, `url`) or "there is a new answer to your request: topic" for a
request (`request_reminder`, data `user`, `topic`, `url`). When the answer itself already reached
that channel, there is no reminder; an answer the messenger did not take (the bot is blocked) does
not count. Several answers in a row make one reminder until the user writes. When the chosen
channel does not work (the messenger is not linked, the bot has no token, the bot is blocked,
Discord refuses direct messages, the email did not go), the reminder tries the others in turn —
Telegram, Discord, email — leaving out the channel the answer reached, and stops at the first that
takes it; the delivery log shows the errors of every one tried. When the user has no channel but
the one the answer reached, there is no reminder at all. See [Support requests](Support-and-incidents.md).
 When a request waits for the user, is resolved or closed, the user hears of it where they chose (`request_status`; to the messenger of their last message in it, if any). These events go only to the user. So do the decisions on a user's article suggestion: `suggestion_accepted` (with a link to the published page) and `suggestion_rejected` (with the reason), see [Guides](Guides-and-releases.md).

**Sign-in alerts.** A sign-in from a new network or browser is told only to the account owner
(`sign_in_new_place`, data `user`, `device`, `address`, `url` — the sessions of the profile):
always to administrators and moderators, to a portal user when they turned it on in
**Profile → Notifications**. A lockout after wrong passwords goes to the channels subscribed to
`sign_in_locked`, once per lockout, without the password. See [Security](Security.md#sign-in-alerts).

**Report to the staff.** In **Settings → Report to the staff** an administrator turns on a
summary every day or every week (off by default). The daily one goes in the first hour after
9:00 by the panel time, the weekly one on Monday at the same time (or on the day it is turned
on). It says which servers do not answer, which relays the panel cannot read and which groups
have no member answering the relay, the traffic of the period with the five most active keys,
the clients whose access ends within a week, the operations that failed, the time of the
oldest protocol backup and of the latest panel archive, the applications waiting for approval
and the new support requests. It is made of what the panel already knows: no server is asked
anything, and nothing in it names a site a user visited. The channels subscribed to
`staff_report` get the whole panel. Each moderator gets a report of their own zone
(`zone_report`, only to them, by email or their messenger, without relays, groups, the
archive and applications): their servers, their clients' keys and requests, the operations they
started. **Send now** under the setting sends the report at once to check how it reads, a daily
one while reports are off; the result is in the delivery log.

Every message is written in the language of whoever reads it. A user picks the language of
their notifications on the **Messengers** page; until they do, they get the language of the
site (Russian). Channels always get the language of the site, whoever the event is about.
Switching RU/EN in the panel header sets this language too. Webhook `data` does not depend on
the language: panel words in it (the `type` of a request) are in English, and codes (the `reason`
of a limit) stay codes, while a message shows them in words: "the traffic of the period is used
up", "the traffic in total is used up", "the access term is over".

Chat messages one user sends within two minutes make one notification. It goes out two minutes
after the first message, with every message on a line of its own in `text`; `count` in `data`
says how many there are.

A server that goes down is reported once; it is reported again only after it has come back and gone
down again. When it answers again, `server_up` is sent.

When the amneziawg kernel module on a server is older than 3.1, amnezia-awg2 runs without the
AmneziaWG 3.1 settings and clients with them do not connect. The monitor reports it once with
`awg3_dropped`, saying what to do: update or remove the amneziawg module on the host and restart
amnezia-awg2. When the settings are back, `awg3_restored` comes once. A drop on a server whose
amnezia-awg2 is removed is forgotten without a notification.

The monitor also watches the CPU, memory and disk of every server against the thresholds in
**Settings → Server load** (90% each by default, 0 turns a resource off). A short spike wakes
nobody: `server_load_high` comes after several readings in a row at or above the threshold
(3 by default), and `server_load_normal` after as many readings below it, so a load swinging
around the threshold changes nothing. While the load stays high, `server_load_high` is repeated
no more often than set (every 6 hours by default); a load that goes back above the threshold
sooner is not told again. `server_load_normal` comes once per run, only when its start was told.
Each resource is watched apart. Relays are not watched: the panel reads only their traffic.

When a WireGuard-family container runs but its interface is gone for 3 readings in a row (e.g.
the kernel killed amneziawg-go when memory ran out), `interface_lost` comes once and the panel
restarts the container, at most 3 times with a growing pause. `interface_restored` comes once
when a reading finds the interface back; `interface_repair_failed` comes once when the restarts
did not help, and the panel stops restarting. `since` is the first reading without the
interface, `attempts` the restarts made. A container stopped by the administrator ends the watch
without a notification ([Servers and import](Servers-and-import.md#monitoring)).

## In the browser

A user turns on notifications in **Profile → Notifications → In the browser** with
**Get notifications in this browser**: the browser asks for permission and then shows the panel's
notifications even when the panel is closed. This works in desktop browsers, on Android, and on
iPhone and iPad from iOS 16.4 when the panel is added to the Home Screen and opened from there.
Each browser is turned on separately, up to 10 per user; **Send a test** checks this one,
**Turn off in this browser** stops it. Signing out of the panel turns notifications off in that
browser, so the next person to use it does not get them.

A user gets in the browser the same events as by email or messenger: the end of access, the limit,
a support answer and the rest that goes to them. A support answer that has already reached a
messenger (`chat_answer`, `request_answer`) is not pushed. A click opens the page the
notification is about: usage for the limit, the support chat for an answer, the request in the
inbox for a new request.

For administrators there is the **Browser push** channel: events routed to it reach the browsers
of every active administrator who turned notifications on in their profile — for example a new
request, a pending registration or an incident.

The panel makes its own VAPID key pair on first use; the private key is stored encrypted, like the
other secrets. Pushes go only from the background worker, through the browser's push service
(Google, Mozilla, Apple), encrypted for that browser (RFC 8291): the service does not see the
text. A subscription the push service reports as gone (HTTP 404 or 410) is deleted. The address
of a push service is checked like a webhook address: only a public `https` one is accepted.

## Delivery and the log

Messages are sent in the background. Each channel is delivered to separately: a channel that fails
does not hold up the others. A temporary failure (no connection, timeout, HTTP 408, 425, 429 or
5xx) is retried with a growing pause of 30 seconds, then 1, 2 and 4 minutes: 5 attempts in all.
Other errors fail at once.

**Log** shows every delivery: time, event, channel, status, attempts and the error. One event is
never delivered to the same channel twice. A delivery left "sending" by a stopped worker is sent
again after 10 minutes, or marked failed when it has no attempts left. Sent and failed deliveries
are deleted after `LAMNEZIA_NOTIFICATION_RETENTION_DAYS` days (90 by default).

## Webhooks

Request format, events, JSON schema, signature check and receiver examples: [Webhooks](Webhooks.md).
