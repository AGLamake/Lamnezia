# Lamnezia

A self-hosted web panel for managing Amnezia VPN servers: import of servers set up in the Amnezia
app, key issuing, plans and limits, traffic statistics, support and a client portal. The panel keeps
the server files in the format of the Amnezia app, so the app keeps working with the same servers.

> Lamnezia is an independent project and is not affiliated with the Amnezia VPN team.

[Русская версия](../ru/Home.md)

## Administration

- [Installation](Installation.md): requirements, one-command install, HTTPS.
- [Configuration](Configuration.md): every `LAMNEZIA_*` variable and the panel settings.
- [Upgrade](Upgrade.md): updating and releases.
- [Backup](Backup.md): panel backups, restoring the panel and a VPN server.
- [Security](Security.md): roles, two-factor login, secrets, checklist.
- [Troubleshooting](Troubleshooting.md): common errors and FAQ for the administrator.

## Features

- [Servers and import](Servers-and-import.md): adding a server, import from the Amnezia app, monitoring.
- [Protocols](Protocols.md): AmneziaWG 2.0, its parameters and the container.
- [Keys](Keys.md): issuing, showing, disabling, differences with servers.
- [Users, plans and limits](Users-plans-limits.md): plans, personal limits, bulk actions.
- [Registration modes](Registration-modes.md): invites, open registration, approval.
- [Traffic and stats](Traffic-and-stats.md): counting, statistics, Prometheus.
- [Support and incidents](Support-and-incidents.md): requests, chat, inbox, incidents.
- [Notifications](Notifications.md): channels, events, routing; [Webhooks](Webhooks.md).
- [Guides and releases](Guides-and-releases.md): install guides, Amnezia downloads, FAQ.
- [Client portal](Portal.md) and [Audit log](Audit.md).
- [Telegram and Discord](Messengers.md): linking and the bots; [Setting up the bots](Bots.md) from scratch.

## Development

- [Architecture](Architecture.md): diagrams of deployment, layers and the main flows.
- [Development](Development.md): local setup, checks, translations, releases.
- [Code style](Code-style.md): the code standard.
- [Billing plugins](Billing-plugins.md): connecting a payment service, the example plugin.
