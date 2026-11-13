# Development

How to run the panel locally, check a change and send it. Contribution rules:
[CONTRIBUTING.md](../../../CONTRIBUTING.md).

## Local setup

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 itself), optionally `make`
and Docker (PostgreSQL and Redis), GNU gettext for translations.

```bash
uv sync                          # install dependencies into .venv
cp .env.example .env             # set LAMNEZIA_DEBUG=True, uncomment LAMNEZIA_ALLOWED_HOSTS
# set LAMNEZIA_ENCRYPTION_KEY in .env, see Configuration
uv run python manage.py migrate
uv run python manage.py createadmin --username admin
uv run python scripts/tailwind.py # build CSS (downloads the Tailwind CLI once)
uv run uvicorn config.asgi:application --reload
```

Demo data for screenshots and trying the interface: `uv run python manage.py seed_demo`. It adds
three servers with readings, AWG 2 protocols, plans, users with keys and traffic, tickets, a chat
and notifications. Nothing goes over SSH, the addresses are documentation ones. Running it again
adds nothing. Logins: `admin`, `moderator`, `anna`, `boris`, `vera`, `gleb`, password
`demo-password`. With `LAMNEZIA_DEBUG` off the command refuses; `--force` runs it anyway (never
on a production database).

The panel opens at http://127.0.0.1:8000/. Without `LAMNEZIA_DATABASE_URL` it uses SQLite.
PostgreSQL and Redis for development: `docker compose -f compose.dev.yaml up -d` (the URLs are in
`.env.example`). With `LAMNEZIA_DEBUG=True` the UI component showcase is at `/dev/ui/`; while
editing templates keep `uv run python scripts/tailwind.py --watch` running.

The same with `make`: `make dev`, `make css`, `make css-watch`, `make services`.

## Project layout

| Path | What lives there |
|---|---|
| `amnezia_core/` | Amnezia formats (`awg0.conf`, `clientsTable`, `vpn://`, XRay) in plain Python, without Django or network. |
| `apps/ssh/` | SSH connections, host key pinning, the server lock. |
| `apps/operations/` | Background operations and their journal; every SSH change runs here. |
| `apps/servers/`, `apps/protocols/`, `apps/keys/` | Servers, protocol instances, keys. |
| `apps/plans/`, `apps/traffic/`, `apps/accounts/` | Plans and limits, traffic, users and registration. |
| `apps/portal/`, `apps/guides/`, `apps/support/` | Client portal, guides and FAQ, support. |
| `apps/notifications/`, `apps/audit/`, `apps/backups/` | Notifications, audit log, backups. |
| `apps/core/`, `apps/dashboard/`, `apps/bots/` | Panel settings and mail, admin dashboard with users and statistics, messenger bots (in progress). |
| `config/` | Django settings, ASGI, Celery. |
| `templates/`, `assets/`, `locale/` | Templates, CSS sources, translations. |
| `docs/` | Design, architecture, code style and this wiki. |

Details: [Architecture](Architecture.md), [docs/DESIGN.md](../../DESIGN.md).

## Checks

Run before every commit (CI runs the same):

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

`make lint` runs the first three, `make test` the tests, `make coverage` the coverage of
`amnezia_core`, `make format` fixes the formatting. SSH integration tests run in CI against a test
sshd container.

## Interface

Pages are server-side Django templates; HTMX swaps fragments, Alpine (the CSP build) keeps small
state, and Tailwind 4 builds the CSS from `assets/css/app.css` (`make css`).

- **Tokens** are the `--ln-*` variables at the top of `app.css`; the dark theme redefines the same
  ones. Add a new colour as a token, not as a class like `text-blue-600`.
- **Components** are the `btn`, `field`, `table-cards`, `dialog`, `toast`, `badge`, `empty`,
  `skeleton`, `stat`, `tabs`, `choice` (a radio as a card), `card-link`, `progress`, `qr`,
  `link-danger` (a dangerous action shown as text) and `palette` classes plus
  `templates/components/`. The `{% icon "key" %}`, `{% sparkline values %}` and
  `{% bar_chart labels series names label %}` (stacked SVG bars of byte counts, with the numbers
  as a table for screen readers) tags live in `apps/core/templatetags/ui.py`; the charts are drawn
  on the server, there is no chart library. Every component is on the `/dev/ui/` showcase (only
  with `DEBUG`): check both themes and a 360 px width there.
- **A page** starts with a `page-header`: the `page-title`, one `page-lead` sentence on what the
  page is for, and the `page-actions` on the right. Key numbers go in a `kpis` row of `stat` tiles,
  object facts in a `facts` list, short lists in `rows` (a `row` with a coloured `row-lead` icon,
  `row-main`, `row-title`, `row-sub`), rare things fold into a `disclosure`. An empty list is
  `components/empty_state.html` with an icon, a sentence on what will appear there and the action that
  adds the first item.
- **Lists** keep their filters in the URL: `{% include "components/filters.html" with form=… %}`
  (a GET form, folded on phones unless something is filtered; `active_filters` counts the filled
  fields) and `{% include "components/pagination.html" %}` for the pages — it builds the links with
  Django's `{% querystring page=… %}`, so the filters stay. The same action looks the same
  everywhere: the main action of a page is a `btn-primary` on the right of its title, bulk actions
  sit in a `bulk-bar` above the table, quick actions of a row are icon buttons in its last column.
- **The admin menu** is `templates/components/admin_nav.html`, in groups: Overview and Statistics,
  Infrastructure, Clients, Support, System. The red and grey counters next to the items come from
  the `{% attention %}` tag (`apps/dashboard/templatetags/attention.py`); the same
  `apps/dashboard/services/attention.py` builds the **Needs attention** list on the overview, and
  `services/overview.py` its key numbers. Every `Notice` has a `key` by which
  `services/snooze.py` hides it until tomorrow.
- **Rows per page** is `apps/core/paging.py`: `PageSizeMixin` on a `ListView` takes the size from
  `?per_page=` or the session, and `components/pagination.html` shows the choice.
- **Quick search** (Ctrl+K) is `templates/components/command_palette.html` in the admin layout; the
  results come from `dashboard:search` (`apps/dashboard/services/palette.py`), so a new kind of
  object is a new group in `QuickSearch`.
- **Layouts** are `templates/layouts/admin.html`, `portal.html` and `auth.html`; breadcrumbs go in
  the `breadcrumbs` block (`<li><a …>Section</a></li><li aria-current="page">Name</li>`). The
  portal layout shows the portal tabs on every page by itself; a section with its own pages adds
  a second row of tabs in the `subnav` block (see `templates/guides/base.html`).
- **Letters** extend `templates/email/base.html` (tables and inline styles, as mail clients need)
  and go out as text plus HTML: `foo.txt` and `foo.html` side by side.
- **Comments** in templates: `{# … #}` only on one line, `{% comment %}` for longer ones —
  Django prints a `{#` comment that runs on to the next line as page text.
- **No JavaScript in attributes.** The CSP forbids `eval` and inline scripts, so `x-*` attributes
  hold only simple expressions (no `=>`, `;`, `window.`, `Number(`), and behaviour comes from
  attributes handled by `static/js/ui.js`:

  | Attribute | What it does |
  |---|---|
  | `data-confirm="Text"` on a `<form>` | asks for confirmation in a dialog before submitting |
  | `data-toast="success"` + `data-toast-text` | shows a toast |
  | `data-dialog-open="id"`, `data-dialog-close` | opens and closes a `<dialog>` |
  | `data-autosubmit` | submits the form when a field changes |
  | `data-fill="field"` | puts text into a form field |
  | `data-check-all="name"` | ticks every checkbox with that `name` |
  | `data-optimistic="remove"` on an HTMX form | hides its target at once, shows it again if the request fails |

  While a request the user started is on its way, the bar at the top of the page runs and the
  `.btn` that sent it gets `aria-busy="true"` (a spinner) — for ordinary forms, links and HTMX
  alike, with nothing to add in templates. Polling and live updates stay quiet. Moving between
  pages cross-fades (CSS view transitions).

  `config/tests/test_csp.py` checks the templates for violations.

## Translations

Interface strings are written in English and wrapped in gettext. After changing them:

```bash
make messages          # refresh locale/*/django.po
# add the Russian translations to django.po
make messages-check    # fails on untranslated or fuzzy strings
```

`make messages` sorts the catalogs by string, does not wrap lines and leaves out the creation
date, so a run without new strings changes nothing on any gettext version and the diff holds only
the new strings. Strings that the code translates at run time without wrapping them (Discord
command descriptions) are marked with `gettext_noop`, otherwise the run drops them. CI fails when
the committed catalogs differ from a fresh run.

## Interface check

The **ui** CI job starts the panel with demo data (`scripts/ui_seed.py`) and opens the component
showcase `/dev/ui/` and the main pages of a guest, an administrator and a client in both themes at
360 and 1280 px with the real CSP. It fails on serious or critical [axe-core](https://github.com/dequelabs/axe-core)
violations, on a page wider than the window and on any console error. Locally:

```bash
uv run playwright install chromium
curl -sSfL -o /tmp/axe.min.js https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js
# a panel with DEBUG on, an empty database and `python manage.py shell < scripts/ui_seed.py`
uv run python scripts/ui_check.py --axe /tmp/axe.min.js --base http://127.0.0.1:8000
```

## Documentation

When behavior or settings change, update this wiki in `docs/wiki/ru/` and `docs/wiki/en/` (the
same pages in both) and `CHANGELOG.md` in the same commit. Links between pages are relative
(`[Keys](Keys.md)`), so they work both in the repository and in the GitHub Wiki.

The wiki is published by the **Wiki** workflow (`.github/workflows/wiki.yml`) on every push to
`releases` (that is, a release) that touches `docs/wiki/` or the included files (`docs/ARCHITECTURE.md`,
`docs/CODE_STYLE.md`). `scripts/publish_wiki.py` turns the two folders into one flat wiki:
English pages keep their names, Russian pages get the `ru-` prefix, links become wiki links,
`<!-- include: path -->` lines are replaced with the file, and a link that leads nowhere fails the
build. Preview locally:

```bash
uv run python scripts/publish_wiki.py --out /tmp/wiki
```

**Once per repository:** GitHub creates the wiki repository only after the first page is saved by
hand. Turn on **Settings → Features → Wikis**, open the **Wiki** tab, save any first page; the
workflow then replaces it. Re-run the workflow from **Actions → Wiki → Run workflow** if needed.

## Commits and releases

Development goes on the `dev` branch, and pull requests go there; `releases` holds only releases.
Commits follow [Conventional Commits](https://www.conventionalcommits.org/): `feat(keys): ...`,
`fix(ssh): ...`, `docs: ...`. How to publish a release: [CONTRIBUTING.md](../../../CONTRIBUTING.md#releases).
