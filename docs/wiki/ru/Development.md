# Разработка

Как запустить панель локально, проверить изменение и отправить его. Правила участия:
[CONTRIBUTING.md](../../../CONTRIBUTING.md).

## Локальный запуск

Нужны [uv](https://docs.astral.sh/uv/) (сам ставит Python 3.12), по желанию `make` и Docker
(PostgreSQL и Redis), GNU gettext для переводов.

```bash
uv sync                          # зависимости в .venv
cp .env.example .env             # LAMNEZIA_DEBUG=True, раскомментировать LAMNEZIA_ALLOWED_HOSTS
# задать LAMNEZIA_ENCRYPTION_KEY в .env, см. Настройка
uv run python manage.py migrate
uv run python manage.py createadmin --username admin
uv run python scripts/tailwind.py # собрать CSS (один раз скачает Tailwind CLI)
uv run uvicorn config.asgi:application --reload
```

Демо-данные для скриншотов и проверки интерфейса: `uv run python manage.py seed_demo`. Команда
добавляет три сервера с показаниями, протоколы AWG 2, тарифы, пользователей с ключами и
трафиком, обращения, чат и уведомления. По SSH ничего не идёт, адреса — из документации.
Повторный запуск ничего не добавляет. Логины: `admin`, `moderator`, `anna`, `boris`, `vera`,
`gleb`, пароль `demo-password`. При выключенном `LAMNEZIA_DEBUG` команда отказывается;
`--force` запускает её всё равно (никогда — на рабочей базе).

Панель откроется на http://127.0.0.1:8000/. Без `LAMNEZIA_DATABASE_URL` используется SQLite.
PostgreSQL и Redis для разработки: `docker compose -f compose.dev.yaml up -d` (адреса есть в
`.env.example`). При `LAMNEZIA_DEBUG=True` витрина UI-компонентов — на `/dev/ui/`; пока правите
шаблоны, держите запущенным `uv run python scripts/tailwind.py --watch`.

То же через `make`: `make dev`, `make css`, `make css-watch`, `make services`.

## Структура проекта

| Путь | Что там |
|---|---|
| `amnezia_core/` | Форматы Amnezia (`awg0.conf`, `clientsTable`, `vpn://`, XRay) на чистом Python, без Django и сети. |
| `apps/ssh/` | SSH-подключения, закрепление ключей хостов, блокировка сервера. |
| `apps/operations/` | Фоновые операции и их журнал; все изменения по SSH идут здесь. |
| `apps/servers/`, `apps/protocols/`, `apps/keys/` | Серверы, экземпляры протоколов, ключи. |
| `apps/plans/`, `apps/traffic/`, `apps/accounts/` | Тарифы и лимиты, трафик, пользователи и регистрация. |
| `apps/portal/`, `apps/guides/`, `apps/support/` | Клиентский портал, инструкции и FAQ, поддержка. |
| `apps/notifications/`, `apps/audit/`, `apps/backups/` | Уведомления, журнал действий, бэкапы. |
| `apps/core/`, `apps/dashboard/`, `apps/bots/` | Настройки панели и почта, админский дашборд с пользователями и статистикой, боты мессенджеров (в работе). |
| `config/` | Настройки Django, ASGI, Celery. |
| `templates/`, `assets/`, `locale/` | Шаблоны, исходники CSS, переводы. |
| `docs/` | Дизайн, архитектура, стандарт кода и эта wiki. |

Подробнее: [Архитектура](Architecture.md), [docs/DESIGN.md](../../DESIGN.md).

## Проверки

Запускайте перед каждым коммитом (CI запускает то же самое):

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

`make lint` запускает первые три, `make test` — тесты, `make coverage` — покрытие
`amnezia_core`, `make format` исправляет оформление. Интеграционные SSH-тесты идут в CI против
тестового контейнера sshd.

## Интерфейс

Страницы — серверные шаблоны Django; HTMX подменяет фрагменты, Alpine (CSP-сборка) держит мелкое
состояние, CSS собирает Tailwind 4 из `assets/css/app.css` (`make css`).

- **Токены** — переменные `--ln-*` в начале `app.css`; тёмная тема переопределяет их же. Новые
  цвета заводите токеном, а не классом вроде `text-blue-600`.
- **Компоненты** — классы `btn`, `field`, `table-cards`, `dialog`, `toast`, `badge`, `empty`,
  `skeleton`, `stat`, `tabs`, `choice` (радиокнопка-карточка), `card-link`, `progress`, `qr`,
  `link-danger` (опасное действие в виде текста), `palette` и шаблоны `templates/components/`.
  Теги `{% icon "key" %}`, `{% sparkline values %}` и `{% bar_chart labels series names label %}`
  (столбцы байтов в SVG, стопкой, с цифрами таблицей для экранных читалок) — в
  `apps/core/templatetags/ui.py`; графики рисует сервер, библиотеки графиков нет. Все компоненты
  собраны на витрине `/dev/ui/` (только при `DEBUG`): проверяйте там обе темы и ширину 360 px.
- **Страница** начинается с `page-header`: `page-title`, одна фраза `page-lead` о назначении
  страницы и `page-actions` справа. Ключевые цифры — строка `kpis` из плиток `stat`, свойства
  объекта — список `facts`, короткие списки — `rows` (`row` с цветной иконкой `row-lead`,
  `row-main`, `row-title`, `row-sub`), редкое сворачивается в `disclosure`. Пустой список —
  `components/empty_state.html` с иконкой, фразой о том, что здесь появится, и действием, которое добавляет
  первый элемент.
- **Списки** держат фильтры в URL: `{% include "components/filters.html" with form=… %}` (форма
  GET, на телефоне свёрнута, если ничего не отфильтровано; `active_filters` считает заполненные
  поля) и `{% include "components/pagination.html" %}` для страниц — ссылки строит
  `{% querystring page=… %}` Django, так что фильтры сохраняются. Одинаковое действие везде выглядит
  одинаково: главное действие страницы — `btn-primary` справа от заголовка, массовые действия —
  `bulk-bar` над таблицей, быстрые действия строки — кнопки-иконки в последнем столбце.
- **Меню админки** — `templates/components/admin_nav.html`, по группам: Обзор и Статистика,
  Инфраструктура, Клиенты, Поддержка, Система. Красные и серые счётчики у пунктов даёт тег
  `{% attention %}` (`apps/dashboard/templatetags/attention.py`); тот же
  `apps/dashboard/services/attention.py` строит список **Требует внимания** на обзоре, а
  `services/overview.py` — его ключевые цифры. У каждого `Notice` есть `key`, по которому
  `services/snooze.py` прячет его до завтра.
- **Строки на странице** — `apps/core/paging.py`: `PageSizeMixin` в `ListView` берёт размер из
  `?per_page=` или сессии, `components/pagination.html` показывает выбор.
- **Быстрый поиск** (Ctrl+K) — `templates/components/command_palette.html` в каркасе админки;
  результаты отдаёт `dashboard:search` (`apps/dashboard/services/palette.py`), новый вид объектов —
  новая группа в `QuickSearch`.
- **Каркасы** — `templates/layouts/admin.html`, `portal.html`, `auth.html`; хлебные крошки — блок
  `breadcrumbs` (`<li><a …>Раздел</a></li><li aria-current="page">Имя</li>`). Каркас портала сам
  показывает вкладки портала на каждой странице; раздел со своими страницами добавляет второй ряд
  вкладок в блоке `subnav` (пример — `templates/guides/base.html`).
- **Письма** наследуют `templates/email/base.html` (таблицы и инлайн-стили, как требуют почтовые
  клиенты) и уходят текстом и HTML: `foo.txt` и `foo.html` рядом.
- **Комментарии** в шаблонах: `{# … #}` — только в одну строку, длинные — `{% comment %}`: Django
  выводит `{#`-комментарий, перешедший на следующую строку, как текст страницы.
- **Без JavaScript в атрибутах.** CSP запрещает `eval` и инлайн-скрипты, поэтому в `x-*` пишите
  только простые выражения (без `=>`, `;`, `window.`, `Number(`), а поведение — через атрибуты,
  которые обрабатывает `static/js/ui.js`:

  | Атрибут | Что делает |
  |---|---|
  | `data-confirm="Текст"` на `<form>` | спрашивает подтверждение в диалоге перед отправкой |
  | `data-toast="success"` + `data-toast-text` | показывает всплывающее сообщение |
  | `data-dialog-open="id"`, `data-dialog-close` | открывает и закрывает `<dialog>` |
  | `data-autosubmit` | отправляет форму при изменении поля |
  | `data-fill="поле"` | подставляет текст в поле формы |
  | `data-check-all="имя"` | отмечает все чекбоксы с этим `name` |
  | `data-optimistic="remove"` на HTMX-форме | сразу скрывает её цель и возвращает, если запрос не удался |

  Пока идёт запрос, начатый пользователем, вверху страницы бежит полоса, а `.btn`, который его
  отправил, получает `aria-busy="true"` (спиннер) — для обычных форм, ссылок и HTMX, без
  добавлений в шаблонах. Опрос и живые обновления идут тихо. Переход между страницами —
  плавная смена (CSS view transitions).

  `config/tests/test_csp.py` проверяет шаблоны на нарушения.

## Переводы

Строки интерфейса пишутся на английском и оборачиваются в gettext. После их изменения:

```bash
make messages          # обновить locale/*/django.po
# добавить русские переводы в django.po
make messages-check    # падает на непереведённых и fuzzy-строках
```

`make messages` сортирует каталоги по строке, не переносит строки и не пишет дату создания, поэтому
запуск без новых строк ничего не меняет на любой версии gettext, а в диффе только новые строки.
Строки, которые код переводит во время работы без обёртки (описания команд Discord), помечены
`gettext_noop`, иначе запуск их выбросит. CI падает, если каталоги в репозитории отличаются от
свежего запуска.

## Проверка интерфейса

Задача CI **ui** запускает панель с демо-данными (`scripts/ui_seed.py`) и открывает витрину
компонентов `/dev/ui/` и основные страницы гостя, администратора и клиента в обеих темах на 360 и
1280 px при настоящей CSP. Она падает на нарушениях [axe-core](https://github.com/dequelabs/axe-core)
уровня serious и critical, на странице шире окна и на любой ошибке в консоли. Локально:

```bash
uv run playwright install chromium
curl -sSfL -o /tmp/axe.min.js https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js
# панель с DEBUG, пустой базой и `python manage.py shell < scripts/ui_seed.py`
uv run python scripts/ui_check.py --axe /tmp/axe.min.js --base http://127.0.0.1:8000
```

## Документация

Когда меняется поведение или настройки, обновите эту wiki в `docs/wiki/ru/` и `docs/wiki/en/`
(одни и те же страницы в обеих) и `CHANGELOG.md` в том же коммите. Ссылки между страницами
относительные (`[Ключи](Keys.md)`), поэтому работают и в репозитории, и в GitHub Wiki.

Wiki публикует workflow **Wiki** (`.github/workflows/wiki.yml`) при каждом push в `releases` (то есть
при релизе), который затрагивает `docs/wiki/` или подключаемые файлы (`docs/ARCHITECTURE.md`,
`docs/CODE_STYLE.md`). `scripts/publish_wiki.py` превращает две папки в одну плоскую wiki: английские
страницы сохраняют имена, русские получают префикс `ru-`, ссылки становятся ссылками wiki, строки
`<!-- include: путь -->` заменяются файлом, а ссылка в никуда роняет сборку. Посмотреть локально:

```bash
uv run python scripts/publish_wiki.py --out /tmp/wiki
```

**Один раз на репозиторий:** GitHub создаёт репозиторий wiki только после того, как первая
страница сохранена вручную. Включите **Settings → Features → Wikis**, откройте вкладку **Wiki**,
сохраните любую первую страницу — workflow потом её заменит. При необходимости запустите workflow
вручную: **Actions → Wiki → Run workflow**.

## Коммиты и релизы

Разработка идёт в ветке `dev`, pull request — туда же; в `releases` только релизы.
Коммиты по [Conventional Commits](https://www.conventionalcommits.org/ru/): `feat(keys): ...`,
`fix(ssh): ...`, `docs: ...`. Как выпустить релиз: [CONTRIBUTING.md](../../../CONTRIBUTING.md#releases).
