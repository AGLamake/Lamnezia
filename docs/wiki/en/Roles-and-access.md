# Roles and access

Who works in the panel and what each of them sees and may do: the roles, the levels of
the sections, the zones of server administrators and moderators, invites and the bot
commands.

[Русская версия](../ru/Roles-and-access.md)

## Roles

| Role | Access |
|---|---|
| **Administrator** | Everything, including the panel settings and the Django admin site `/admin/`. |
| **Server administrator** | Their own servers whole — protocols, settings, backups, operations — and their own plans, users and moderators; orders and the audit log of their zone. Never the settings, server groups, relays or the Django admin site. |
| **Moderator** | The sections of the admin interface their access opens (see below), narrowed to their zone; never the settings or the Django admin site. |
| **User** | The client portal: own keys, guides, support. |

The role is set in **Admin → Users**.

## Access to sections

Each section of the admin interface has one of three levels for a staff member:

- **none** — the section is not in the menu, the tabs or the quick search (Ctrl+K), and its pages
  answer 403;
- **view** — the pages open, but without action buttons and without key secrets (no key display,
  no `vpn://`, no files, no subscription link); any change answers 403 and touches nothing;
- **manage** — everything the section can do.

Sections: overview, statistics, servers (with groups, relays, protocols and incidents), backups,
operations, users, keys, registration, plans, orders, inbox (with article suggestions), articles
(with reply templates), audit log and staff. The settings, integrations, updates and the panel
move stay with administrators whatever the levels. Each staff member issues their own
[API](API.md) tokens, within their zone and their levels. An administrator manages every
section. Levels are set on the page of a staff member in the **Staff** section.

A section with no chosen level gets the default of the role, and no level goes above the ceiling
of the role. A staff member never gives anyone more than they have themselves. The moderator's
defaults are exactly what a moderator could do in 1.0.0:

| Section | Default | Ceiling |
|---|---|---|
| Overview, statistics | view | view |
| Servers, backups, plans | none | view |
| Operations, users, keys, registration, inbox | manage | manage |
| Orders, articles, audit log, staff | none | none |

A server administrator's defaults:

| Section | Default | Ceiling |
|---|---|---|
| Overview, statistics, servers, backups, operations, users, keys, registration, inbox, plans, staff | manage | manage |
| Orders, audit log | view | view |
| Articles | none | manage |

The start page `/` leads to the first section of the menu the member may open.

## Server administrator's zone

A server administrator runs their own servers, like a separate panel inside the panel. Their zone
is the servers they added or were given, the plans they staff, the users they serve and the
moderators who share their servers or plans. Anything else answers 404 and is left out of every
list, filter, counter, **Needs attention**, statistics and the quick search (Ctrl+K):

- **Servers**: a server they add is theirs at once (they join its staff members). They install
  protocols, change settings, restore backups and see the operations of their servers.
- **Plans**: a plan they create is theirs; it takes only their servers, and at least one must be
  chosen (a plan with no servers works on all of them). Servers of the plan outside their zone stay
  in it untouched. Their users are given only their plans.
- **Orders**: the orders of their plans, view only.
- **Audit log** and **operations**: their own actions and those of their moderators.
- **Users**: a user served by two server administrators is seen by both, each with only the keys
  on their own servers.
- **Staff**: they give section levels only to their moderators, never above their own.
- **Registration**: only their own invites, with only their plans; a user who comes by one
  joins their zone at once.

Server groups, relays and split lists join servers across the panel, so they belong to
administrators only; a moderator or a server administrator does not see them. Payment details and
articles stay shared across the panel.

**Deleting a server.** The server page has **Delete** for an administrator and for the only server
administrator of the server. The panel forgets the server and touches nothing on it, so the server
must have no protocols or services left, and no relay of server groups: an administrator removes
the relay first ([Server groups](Server-groups.md)). A server shared by several server administrators cannot be
deleted by one of them: each of them can **Leave** its list instead, and it stays with the others.

## Moderator's zone

A moderator serves their own users on their own servers, as a reseller does. It is set up in the
**Staff** section of the panel:

- **Add a staff member** creates an account: an administrator makes server administrators and
  moderators, a server administrator makes only moderators, who stay theirs. **Change the password
  at the next sign-in** (on by default) makes the new member choose their own password first;
- the page of a member sets **Users at most** and **Keys at most** (empty: no limit), their servers
  and plans, and the level of each section; an administrator also changes the role there. A server
  administrator is offered only their own servers and plans and levels no higher than their own,
  and manages only their own moderators. The quotas of a moderator a server administrator made are
  no higher than that administrator's own, and empty is refused while theirs is set; this holds for
  an administrator too, also in the admin site, where a role changed in the same form is checked
  as the new one. A moderator an administrator made has the quotas of the server administrator
  whose server they were given. If the server administrator's quota is lowered later, the lower
  of the two applies to the moderator;
- the pages of a server, a plan and a user have a **Staff** block to add or remove a member; a user
  may have several, and is then in the zone of each. A link "from the plan" came with the user's
  plan and follows it, so it cannot be removed by hand;
- **Link to a staff member** in the bulk actions of the user list links the ticked users while the
  member's user quota has room; the report shows who was skipped.

Everything outside the zone is as good as missing for the moderator:

- **Keys**: only the keys of their users on their servers; a key is issued or handed over only to
  their users, on their servers, while their users hold fewer keys than the quota (revoked keys do
  not count). Differences with the server — only on their servers.
- **Operations**: only the ones they started.
- **Support**: only the conversations of their users that are assigned to them or to nobody, and
  only their images. An assignment does not take a conversation into the zone: a request is assigned
  to an administrator or to one of its user's moderators. All moderators of a user see their
  conversations that nobody else has taken.
- **Users**: only their users — the list, the card, the plan and limits, messenger links and
  bulk actions. A bulk reissue or turn-off touches only the keys on their servers; the report of a
  bulk action is seen only by whoever started it.
- **Registration**: only their invites; the approval queue stays with administrators.
- **Plans**: a user is given the administrator's plans (those no server administrator
  staffs) and the plans the moderator staffs, never another server administrator's.
- **Statistics**: traffic, users and keys of their zone only, without the **Storage** section.
- **Dashboard** and the [bot commands](#bot-commands): only their servers and their
  incidents. **Take** on an incident in the bot assigns the moderator only the requests of
  their users that nobody else has.

A user with no moderator is served by administrators only. The user quota is checked when a user is
given to a moderator (each moderator of the user counts them) and when someone registers by
the invite of a moderator or a server administrator: such a user joins their zone at once,
and with the quota used up the invite does not open registration. The key
quota applies wherever a new key is issued: the issue form, the portal and the bots. A key of a
user with several moderators counts for each of them, so a new key stops once any of them has
reached their key quota.

**Staff of a plan.** A plan may have its own staff members (a server administrator who creates a
plan is put on it). A user given the plan — by hand, by payment, by a bulk change of plan or with
the trial plan at registration — is served by each of them; such links are marked "from the plan".
Another plan replaces them with the staff of the new one, taking the plan away drops them, and a
change of the plan's staff relinks all its users at once. Links made by hand are never touched: a
member linked by hand stays so, without a second link. A moderator whose user quota is used up is
skipped, and the audit journal records **Plan staff member skipped: user quota**.

## Bot commands

The staff commands of the bots (**Administration**: server summary, traffic today, incidents and
replies to notifications, see [Setting up the bots](Bots.md#6-commands-for-administrators-and-moderators)) follow the same zone and
levels as the panel:

| Command | Section and level |
|---|---|
| Server summary, traffic today | overview, view |
| Incidents, an incident card | inbox, view |
| **Take** an incident, a reply to a support notification | inbox, manage |
| **Close** an incident | servers, manage |

A command the level does not allow is not offered, and typed by hand it answers "This is not
available to you."; at **view** an incident card has no buttons. Everyone sees only the servers and
incidents of their zone. With the defaults a moderator does what they did in 1.0.0 — summaries,
incidents, **Take** and replies, but no **Close**; a server administrator also closes the incidents
of their servers.

## Update from 1.0.0

The update needs nothing but the migrations. The administrator's access does not change. Each
user's moderator becomes their only staff member, and each moderator gets the defaults above,
which are exactly the rights of 1.0.0: the same pages open, the same users, keys, requests and
invites are seen and the same actions are there, in the panel and in the bots. The one visible
difference: the server filter of the user list and of the inbox offers a moderator only their own
servers.
