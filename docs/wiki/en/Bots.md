# Setting up the bots

Step by step from nothing to a working Telegram bot, Discord bot and a Discord channel with the
administrators' notifications. What the bots do for users is in
[Telegram and Discord](Messengers.md).

Both bots are optional and independent: set up one, the other or both.

## 1. Telegram bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`.
2. Enter a display name (`My VPN`) and a username ending in `bot` (`my_vpn_bot`).
3. BotFather answers with a token like `123456789:AA…`. Keep it secret: whoever has it controls
   the bot. A leaked token is replaced with `/revoke`.
4. Optional, for **Sign in with Telegram** on the login page: send `/setdomain`, choose the bot and
   enter the panel domain (`vpn.example.com`, without `https://`).

The bot sets its command menu and descriptions itself; nothing else is needed in BotFather.

## 2. Discord application and bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and press
   **New Application**; name it as the bot.
2. **General Information**: copy the **Application ID**.
3. **OAuth2**: press **Reset Secret** and copy the **Client Secret**. Under **Redirects** add
   `<panel address>/accounts/discord/callback/`, for example
   `https://vpn.example.com/accounts/discord/callback/`. The panel shows the exact address under the
   Discord fields in its settings. The address must match character for character, including
   `https` and the final `/`.
4. **Bot**: press **Reset Token** and copy the token. **Privileged Gateway Intents** stay off: the
   bot needs none of them. **Public Bot** can be turned off so that only you can invite it.
5. Invite the bot to your server: **OAuth2 → URL Generator**, scopes `bot` and
   `applications.commands`, no bot permissions. Open the generated link and choose the server.

The user must share a server with the bot to get direct messages from it, so invite it to the
server your users are on.

The application ID and the client secret are for linking and signing in with Discord; the bot
token is for the bot itself. Linking with the `/link <code>` command works with the bot token only.

## 3. Tokens in the panel

**Settings → Integrations** (administrators):

| Field | Value |
|---|---|
| Telegram bot token | The token from BotFather. |
| Telegram bot username | The username, without `@`. |
| Discord application ID | **Application ID** from step 2. |
| Discord client secret | **Client Secret** from step 2. |
| Discord bot token | The token from the **Bot** page. |

Save. Tokens and the secret are stored encrypted and are not shown again: an empty field keeps the
saved value. **Sign in with Telegram** and **Sign in with Discord** are off by default; turn them on
here if wanted.

## 4. The bots service

The bots run in the `bots` service of `compose.yaml`; it needs no profile and starts with the panel.
After an upgrade or if it is not running:

```sh
docker compose up -d bots
docker compose logs -f bots
```

A new or changed token is picked up within a few seconds, without a restart. Under the settings
form the **Bots** block should show both bots as **running** with their `@username`, and the
Discord servers the bot is on. **No token** means the field is empty, **error** gives the reason
(usually a wrong token), and **not responding** means the `bots` container is stopped or cannot
reach the database. **Turn off**, **Turn on** and **Restart** are in the same block.

Check: open the bot in Telegram and send `/start`, or run `/menu` on the Discord server. The
Discord bot opens the panel in your direct messages and answers on the server with a note only you
see; only `/menu` and `/link` are offered on servers. If your direct messages are closed, the bot
explains how to open them in the server's privacy settings. An unlinked account gets an offer to
link it; link it on the **Messengers** page of your profile.

Telegram updates come by long polling by default, which works behind NAT and without a domain.
Webhook mode for a public HTTPS domain: see [The Telegram bot](Messengers.md#the-telegram-bot).

## 5. Discord channel for the administrators' notifications

Notifications to a Discord channel go through a channel webhook and need no bot.

1. In Discord: **Edit Channel → Integrations → Webhooks → New Webhook**, choose the channel and
   press **Copy Webhook URL**.
2. In the panel: **Admin → Notifications** → add a channel of kind **Discord** and paste the URL.
3. Tick the events the channel should receive (servers down, new requests, chat messages…) and save.
4. **Send test**: a test message should come to the channel; the result is in the log on the same
   page.

For a single administrator a channel of kind **Discord direct message** with their Discord user ID
works as well; it is sent by the panel Discord bot. A Telegram chat works the same way with a
**Telegram** channel. See [Notifications](Notifications.md).

## 6. Commands for administrators and moderators

A linked administrator or moderator sees **Administration** in the bot menu (or types `/admin`):

- **Server summary**: each server online or not, CPU, memory and disk load, maintenance and open
  incidents.
- **Traffic today**: traffic since midnight in the panel time zone, in total and by server.
- **Incidents**: open incidents with the number of reports. **Take** assigns the incident's open
  requests to you and puts new ones in work, so the users hear someone is on it. **Close** closes
  the incident and its requests and tells the users; as on the incidents page, only an
  administrator closes, after a confirmation. For a moderator, **Take** assigns only the requests
  of their users that nobody else has.

To answer a user, reply (the messenger's **Reply**) to the bot notification about a new request
or chat message: the text goes to that request or chat as the staff's answer. The notification
title carries the number, `#12`. A moderator answers only the requests of their users and sees only their servers and incidents
([moderator's zone](Security.md)).

Users without the administrator or moderator role get "This is not available to you." for all of
these commands, and their replies to the bot are ordinary messages.

## If something does not work

- **Telegram link opens telegram.org**: the bot username is empty or wrong in the settings.
- **Discord says "Invalid OAuth2 redirect_uri"**: the redirect in the Developer Portal differs from
  the one shown in the panel settings.
- **No slash commands in Discord**: wait a minute after the first start, then reopen Discord; check
  that the invite had the `applications.commands` scope.
- **A user gets no direct messages from the Discord bot**: they share no server with it, or their
  privacy settings forbid messages from server members.
