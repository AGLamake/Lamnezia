# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Server groups**: AmneziaWG 3.1 servers join a group as clones of its leading server, so one
  key works on each of them. Joining and leaving run as background operations with a snapshot of
  the server files; a server is either standalone or in one group.

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
