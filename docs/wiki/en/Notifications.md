# Notifications

**Admin → Notifications** (`/notifications/`, administrators only) sends panel events to email,
Telegram, Discord and your own webhooks.

## Channels

A channel is one place messages go to. There can be several channels of each kind.

| Kind | What to fill in |
|---|---|
| Email | One or more addresses separated by commas. Letters go through the panel's mail server ([Configuration](Configuration.md)). |
| Telegram | Chat ID of a user, group or channel the bot can write to. Bot token: the channel's own, or empty to use the one from the panel settings. |
| Discord | Webhook URL of a Discord channel (**Channel settings → Integrations → Webhooks**). Messages come as an embed. |
| Discord direct message | The Discord user ID (developer mode → right click → Copy User ID). Sent by the panel Discord bot; the user must share a server with it. |
| Webhook | Your URL and a signing secret. The panel posts JSON (see below). |

Tokens, webhook URLs and secrets are stored encrypted and are not shown again: leave the field
empty to keep the saved value. A disabled channel receives nothing but the test message.

Webhook and Discord URLs must lead to a public address. An address in a private network
(`127.0.0.1`, `10.0.0.0/8`, `192.168.0.0/16`, `169.254.169.254` and the like, also behind a host
name) is refused when the channel is saved and checked again on every send; redirects are not
followed. For a receiver in your own network turn on **Settings → Integrations → Notifications to
private networks**.

**Send test** delivers a test message to the channel at once; the result is in the log.

## Events and routing

On the channel page tick the events the channel should receive. The channel list shows which
channels hear each event.

| Event | `event` | `data` |
|---|---|---|
| Server is down | `server_down` | `server`, `host`, `last_seen` |
| Limit reached | `limit_reached` | `user`, `reason` (`traffic`, `total_traffic`, `expired`) |
| Access ends soon | `access_expiring` | `user`, `until` (date) |
| Keys must be imported again | `keys_changed` | `user`, `server` |
| Keys revoked | `keys_revoked` | `user`, `server` |
| Keys moved to AmneziaWG 3.1 | `keys_moved` | `user`, `server` |
| Registration request | `registration_pending` | `user`, `email` |
| New support ticket | `ticket_created` | `user`, `type`, `subject` |
| Chat message | `chat_message` | `user`, `text`, `count` |
| Incident opened | `incident_opened` | `server`, `reports` |
| Incident closed | `incident_closed` | `server` |
| New article suggestion | `suggestion_created` | `user`, `kind`, `title` |
| Payment received | `payment_received` | `user`, `plan`, `amount`, `currency`, `provider` |

Notifications about the user go where they chose on their **Messengers** page: email by default,
or their linked Telegram or Discord ([Telegram and Discord](Messengers.md)).
Users themselves get an email about their reached limit and three days before their access ends,
if they have an address. When an incident is closed, everyone who reported it gets an email
that the server works again (`incident_resolved`; it goes only to the users, never to a channel).

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
takes it; the delivery log shows the errors of every one tried. See [Support requests](Support-and-incidents.md).
 When a request waits for the user, is resolved or closed, the user hears of it where they chose (`request_status`; to the messenger of their last message in it, if any). These events go only to the user. So do the decisions on a user's article suggestion: `suggestion_accepted` (with a link to the published page) and `suggestion_rejected` (with the reason), see [Guides](Guides-and-releases.md).

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
down again.

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
