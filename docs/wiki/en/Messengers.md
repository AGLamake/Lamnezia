# Telegram and Discord accounts

A user links their Telegram and Discord accounts in the **Messengers** section of their
**Profile** (`/accounts/profile/#messengers`). Either or both can be linked. One
Telegram or Discord account belongs to one user of the panel: an account linked to someone else is
refused until they unlink it.

## Setting up

Step by step, with BotFather and the Discord Developer Portal: [Setting up the bots](Bots.md).

In **Settings → Integrations** (administrators):

| Field | What for |
|---|---|
| Telegram bot token, Telegram bot username | Linking Telegram: the bot opens with a one-time code. Username without `@`. |
| Discord application ID, Discord client secret | Linking Discord with OAuth2. In the Discord Developer Portal add the redirect shown under the fields (`<panel address>/accounts/discord/callback/`). |
| Discord bot token | The Discord bot: keys, the menu and direct messages with notifications. **Bot** page of the same application in the Developer Portal. |
| Sign in with Telegram | Off by default. Shows **Sign in with Telegram** on the login page. The button opens Telegram's own login page (`oauth.telegram.org`), which returns the visitor to the panel; the panel checks Telegram's signature. Send `/setdomain` to @BotFather with the panel domain (shown under the option), otherwise Telegram answers "Bot domain invalid". |
| Sign in with Discord | Off by default. Shows **Sign in with Discord** on the login page. |

The login buttons are in the colours of the messengers, with their logos.

### Offer to link

While a user has no linked messenger and at least one bot is set up, **My access** starts with a
card that offers to link Telegram or Discord: sign in with one tap, get keys and notifications
(access ending, the limit, answers of support) and manage keys right in the messenger. Its
buttons start linking at once; the same card opens the support chat. **Not now** hides it for
30 days, the second time for good. The letter that confirms the email of a new account also
says that the messengers can be linked in the profile.

## Linking

- **Telegram.** **Link account** gives a link `t.me/<bot>?start=<code>`. The bot takes the code and
  links the Telegram account that pressed **Start**.
- **Discord.** **Link account** opens Discord, which asks to share the account name only (scope
  `identify`). **Code for the bot** gives a `/link <code>` command for the panel bot instead.

A code works once and for 10 minutes; a new code cancels the previous one. Only its SHA-256 is kept
in the database. **Unlink** removes the link; the bots will offer the same command.

### By an administrator

The user card in **Users** (`/users/<id>/`, administrators) has a **Linked accounts** block with
each messenger: the name, the ID and when it was linked, and **Unlink** after a confirmation. For a
messenger that is not linked yet the administrator can:

- enter the account ID (the Telegram user ID or the Discord user ID, digits only) and, if wanted,
  the name, and press **Link account**; an account linked to another user is refused;
- press **Link for the user**: the card shows a single-use `t.me` link or a `/link <code>` command
  to send to the user. It is the same code as in the profile, with the same rules.

The **Users** list shows the linked accounts in the **Messengers** column. The **Messenger** filter
leaves users with a Telegram, with a Discord or with nothing linked, and the search also finds the
messenger name (with or without `@`) and the exact account ID.

## Signing in

Signing in with a messenger works only for accounts already linked to a user; it never creates
accounts. Two-factor authentication still asks for its code afterwards. A Telegram answer older than
10 minutes is refused.

## The bots

The panel bots answer only linked accounts: anyone else gets only how to link. A user waiting for
approval or blocked can see their status and the guides. The bots answer in the user's messenger
language (Russian or English) and take up to `LAMNEZIA_BOT_RATE_LIMIT` commands a minute from one
account; a button pressed over the limit shows a short popup and leaves the panel as it is.

| Command | What it does |
|---|---|
| `/start`, `/menu` | The menu with buttons. |
| `/status` | Traffic, the total limit and the access term, as on the portal. |
| `/keys` | The user's keys; a key's card can show, re-issue or delete it. |
| `/newkey` | A new key in steps: the server (when the plan has several), then the device. |
| `/sub` | The subscription link for XRay clients; **New link** replaces it (the old one stops working). Only with the "any time" key policy. |
| `/servers` | Health of the plan's servers. |
| `/down` | "A server does not work": the server, then a comment or none. The panel checks the server and the administrators get a request, as from the portal. |
| `/support` | Any message that answers no question goes to the user's support chat. |
| `/ticket` | A new request in steps: the type, then the description; its first line is the topic. "A server does not work" leads to `/down`. |
| `/requests` | The user's last requests with their status; a request shows its last messages and takes an answer. |
| `/guides` | Links to the app and the guide for each device. |
| `/unlink` | Unlinks the account after a confirmation. |

The bot talks through one panel message. A button redraws that message in place: each section has
a title in bold, a short text or a list, its buttons, and **Back** (to the section above) and
**Menu** at the end. Typed text, a command, and a notification or an answer of the staff that came
after the panel open a new panel at the bottom of the chat. A key (the `vpn://` link, the QR codes,
the `.conf` file) and other files come as messages of their own, with a new panel below them, so
going on through the menu never takes the key away. The panel whose button brought the key loses
its buttons, so only the panel below stays live. Buttons of an older panel still work and redraw
that panel; when it can no longer be edited, the bot posts a new one. A panel longer than one
message of the messenger comes as several messages, with the buttons under the last.

Messengers take only internet addresses on buttons. When `LAMNEZIA_SITE_URL` is not a public domain
(`localhost`, a private IP address, a name without a dot or ending in `.local`, `.lan`,
`.internal`), links to the site come as lines of text instead of buttons, so the bots work in dev
too.

Re-issuing and deleting ask for a confirmation; a second press while the old key is being revoked
does nothing. **Keys in the bots** (**Settings → Users and keys**) decides what a shown key is: the key
itself (the `vpn://` link, the QR codes and the `.conf` file, only in a private chat) or only a link
to the key on the portal. Telegram and Discord keep chats on their servers without end-to-end
encryption; choose the link to keep keys out of them. With the link a key is not counted as shown;
otherwise the "only once" policy is spent by the bot or the portal, whichever shows the key first.

The panel remembers which messenger each support message came from. The staff's answer goes back there from the bot, whether or not the user has the panel open: an answer in the chat can be answered by just writing to the bot, an answer to a request points to `/requests`. A picture attached to an answer is seen only in the personal account. The user may send a picture too (a photo in Telegram, an image attachment in Discord), with a caption or without: it goes to the support chat or, after **Answer**, to the request, and is checked like one attached on the portal. When the user neither answers nor reads it within 2 minutes, the channel they chose for notifications gets one reminder with a link to the thread, unless the answer itself went there ([Notifications](Notifications.md)).

The journal records every command of a linked user by its name only, without arguments or typed
text ([Audit log](Audit.md)).

The bots run in the `bots` service of compose, which always runs along with the panel.

### The Bots block

**Settings**, under the form, has the **Bots** block for administrators. It refreshes itself every
few seconds and shows each bot:

- its state: **running** (and for how long), **turned off**, **no token**, **starting** or
  **error** with the reason (the token is never shown in it); a failed bot is tried again in a
  minute or at once by **Restart**;
- **not responding** when the `bots` service has not reported for a while: the container is stopped
  or cannot reach the database;
- the bot name (`@username`) and how many users linked this messenger;
- for Discord, the servers the bot is on with the number of members, and **Leave** for each.

**Turn off** and **Turn on** keep the choice across restarts; a bot with a token is on by default.
**Restart** reconnects the bot. A changed token restarts the bot by itself within a few seconds,
also when the panel and the `bots` service share no cache.
The panel only writes the request into the database and the `bots` service carries it out; every
action is in the [audit log](Audit.md) (`bots.turn_on`, `bots.turn_off`, `bots.restart`,
`bots.leave`).

### The Discord bot

Invite the bot to a server with the `bot` and `applications.commands` scopes (OAuth2 → URL
Generator in the Developer Portal); it needs no permissions on the server and no privileged
intents. The commands of the menu are slash commands (`/menu`, `/keys`, `/newkey`…), with
descriptions in Russian and English, and `/link <code>` links the account with a code from the
profile. Buttons run the same actions as in Telegram and keep working after a restart of the bot.

Users work with the bot in direct messages: there the answers are ordinary messages, and typed
text goes on to dialog steps and support. On a server only `/menu` and `/link` are offered: the bot
opens the panel in the user's direct messages and answers on the server with an ephemeral note
"I wrote to you in direct messages" that only the user who ran the command sees. Nothing is visible
to others on the server, so a key never shows up in a channel; plain messages on servers are not
read. If the user's direct messages are closed, the bot explains ephemerally how to open them: the
server menu → Privacy Settings → allow direct messages from server members, then run the command
again. A key comes as the `vpn://` link in a code block and a message with the QR code and the
`.conf` file attached, then the panel. Discord may
take a few seconds to show the commands after the bot starts for the first time.

### The Telegram bot

The bot answers only in private chats; it ignores groups, so a key never shows up there. A key comes
as the `vpn://` link in monospace (tap to copy), the QR code as a photo and the `.conf` file as a
document, then the panel. The bot sets its command menu in Russian and English.

By default the bot takes updates by long polling: it works behind NAT and needs no domain. With a
public HTTPS domain set `LAMNEZIA_TELEGRAM_WEBHOOK=true`: Telegram then posts updates to
`<LAMNEZIA_SITE_URL>/bots/telegram/` with a secret header, and the bot sets the webhook itself on
start.

- **Caddy** (`--profile caddy`) passes `/bots/telegram/` to the `bots` service already.
- **nginx on the host**: add the `location /bots/telegram/` block from `deploy/nginx/lamnezia.conf`
  and publish the port of the `bots` service in `compose.override.yaml` next to `compose.yaml`
  (take another host port if 8081 is busy, and the same one in nginx):

  ```yaml
  services:
    bots:
      ports:
        - "127.0.0.1:8081:8081"
  ```

Turning the webhook off again: set `false` and run `docker compose up -d bots`; the bot removes the
webhook itself.

## Notifications

**My notifications** on the same page chooses where the user's own notifications go: email or a
linked messenger. Messages come from the panel bot: in Telegram a private chat, in Discord a direct
message (the user must share a server with the bot), with a **Menu** button that opens the panel.
Without the bot token the user gets email.
Unlinking a messenger switches the
choice back to email. See [Notifications](Notifications.md).

The journal records linking, unlinking and every sign-in with a messenger ([Audit log](Audit.md)).
