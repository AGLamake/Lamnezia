# Keys

A key is one client (peer) of a protocol on a server. Keys are managed in **Keys** in the admin
interface (`/keys/`, administrators and moderators; a moderator only within their
[zone](Security.md)).

## Issuing

**Issue a key**: choose the user, the server and protocol, and a device name. The panel creates the
client keys, adds the peer on the server and records the user in `clientsTable`, so the key is
also visible in the Amnezia app. The page of the operation shows the progress; if it fails,
press **Retry**: the same key is added again, nothing is duplicated. Servers under maintenance
are not offered for new keys.

The client private key is stored encrypted with `LAMNEZIA_ENCRYPTION_KEY`, together with the
server settings needed to show the key later.

## Showing a key

**Show key** gives the `vpn://` link for the Amnezia app, the QR code (a series of codes for
large configs) and a `.conf` file for the AmneziaWG app; an XRay key has a `vless://` link for
other XRay clients instead of the `.conf`, an OpenVPN key has an `.ovpn` file, and an IPsec key has a `.sswan` file for strongSwan on
Android and a `.mobileconfig` profile for iPhone, iPad and Mac. Every look at a key by an administrator
is written to the audit log. **Allow the owner to open it again** resets the first view of the key
for the "show once" policy.

Keys imported from a server were not created by the panel and have no private part: they cannot be
shown, only revoked or re-issued. Imported XRay keys are the exception: the panel knows their UUID,
so the first time such a key is opened it reads the server settings from `server.json` in the
background and the key can be shown a few seconds later; a key in a subscription link joins the
feed on one of the next refreshes.

The Reality shortId is part of every XRay key. The panel keeps the one it last read from
`server.json`; when the shortId changes on the server (for example the protocol was set up anew in
the Amnezia app), the panel notices it on the next key issue or periodic check and updates the
stored profiles, so the keys shown afterwards carry the new one.

## Other actions

- **Disable / Enable** — the peer is taken out of the server config and put back with the same
  address and keys, so the client config keeps working.
- **Revoke** — the peer and its `clientsTable` record are removed; the panel forgets the secrets.
  Until the operation ends the key is *revoking*: it no longer counts towards the limits, and a
  second **Revoke** starts nothing.
- **Reissue** — a new key for the same owner, server and device, made by one `keys.reissue`
  operation; the old key is *revoking* at once. If the new key cannot be made, the old one gets
  its status back.
- **Give to a user** — for keys without an owner (for example imported ones); the owner is also
  written to `clientsTable` on the server.

Every action is a background operation and appears in **Operations**.

**Disable**, **Enable** and **Revoke** also work on several keys at once: tick them in **Keys**,
choose the action above the table and press **Apply**. Each key gets its own operation, and the
panel opens **Operations**; keys whose status does not allow the action are skipped and counted.
A moderator can tick only the keys of their users.

## Differences with servers

The Amnezia app edits the same files as the panel. Every `LAMNEZIA_RECONCILE_INTERVAL` seconds
(15 minutes by default) and on **Keys → Differences with servers → Check now** the panel reads each
server and compares it with its keys. Reading changes nothing on the server.

| Difference | What happened | Actions |
|---|---|---|
| added outside the panel | a client was added in the Amnezia app or by hand | **Add to the panel** — an ownerless key, like an imported one; **Remove from the server** |
| removed outside the panel | an active key has no client on the server | **Restore on the server** — with the same address, the device keeps working; **Mark as revoked** |
| off in the panel, but on the server | a disabled or revoked key is still on the server | **Remove from the server**; **Mark as active** |

Before acting the panel reads the server again: if the difference is already gone, nothing is
changed; if it has turned into another one, the operation stops and asks to check again. Every
action is recorded in the audit journal.

Sources with differences are shown as cards with a table; those that match the panel share one short
card — a line per server and protocol with a **Check now** button.
