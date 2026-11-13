# Contributing to Lamnezia

[English](#english) · [Русский](#русский)

## English

Thank you for your interest in Lamnezia — a self-hosted web panel for Amnezia VPN servers.
Lamnezia is an independent project and is not affiliated with the Amnezia VPN team.

### Before you start

- Read the [Code of Conduct](CODE_OF_CONDUCT.md).
- For bugs and feature ideas, open an issue using one of the templates. For security issues,
  follow [SECURITY.md](SECURITY.md) and do not open a public issue.
- For larger changes, discuss the idea in an issue first.

### Code

- The code style is mandatory: [docs/CODE_STYLE.md](docs/CODE_STYLE.md) (layers, when to create a
  class, formatting, pre-commit checklist).
- Compatibility with the Amnezia app comes first: the formats of `clientsTable`, `awg0.conf` and
  `vpn://` follow upstream `amnezia-client` exactly. All Amnezia format knowledge lives in
  `amnezia_core/`.
- Never log secrets (SSH credentials, private keys) or resources visited by VPN users.
- User interface strings go through gettext (RU and EN). Write the source string in English, run
  `make messages` and add the Russian translation to the updated `django.po`; `make messages-check`
  (CI does the same) fails on untranslated strings. GNU gettext is required.
- Before pushing, run `ruff check`, `ruff format --check`, `mypy` and `pytest`.

### Documentation

If a change affects behavior or settings, update `docs/wiki/` (both `ru/` and `en/`) and
[CHANGELOG.md](CHANGELOG.md) in the same pull request.

### Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat(keys): ...`,
  `fix(ssh): ...`, `docs: ...`. Keep messages short.
- One pull request — one topic. Fill in the pull request template.
- Pull requests go to the `dev` branch, where development happens. The `releases` branch holds
  only releases.

### Releases

Versions follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`, pre-releases
look like `1.0.0-rc.1`. To publish a release:

1. Set the version in `pyproject.toml`.
2. In [CHANGELOG.md](CHANGELOG.md) rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD` and add an empty
   `[Unreleased]` section above it.
3. Commit to `dev` and wait for a green CI.
4. Move `releases` to it, tag and push:
   `git checkout releases && git merge --ff-only dev && git tag vX.Y.Z &&
   git push origin releases vX.Y.Z`.

The tag publishes the image `ghcr.io/aglamake/lamnezia` (tags `X.Y.Z`, `X.Y`, `X` and `latest`;
a pre-release gets only its own tag) and a GitHub release with notes from the changelog section.
The workflow stops if the tag does not match `pyproject.toml` or the changelog has no section for it.

### License

By contributing, you agree that your contributions are licensed under
[AGPL-3.0](LICENSE). Files adapted from `amnezia-client` must keep their original copyright
notice and be listed in [NOTICE](NOTICE).

## Русский

Спасибо за интерес к Lamnezia — self-hosted веб-панели для серверов Amnezia VPN.
Lamnezia — независимый проект и официально не связан с командой Amnezia VPN.

### Перед началом

- Прочитайте [Кодекс поведения](CODE_OF_CONDUCT.md).
- Об ошибках и идеях сообщайте через issue по шаблону. Об уязвимостях — по
  [SECURITY.md](SECURITY.md), не в публичном issue.
- Крупные изменения сначала обсудите в issue.

### Код

- Стандарт кода обязателен: [docs/CODE_STYLE.md](docs/CODE_STYLE.md) (слои, когда заводить класс,
  оформление, чек-лист перед коммитом).
- Совместимость с приложением Amnezia — главное: форматы `clientsTable`, `awg0.conf` и `vpn://`
  повторяют upstream `amnezia-client` 1:1. Всё знание о форматах Amnezia — в `amnezia_core/`.
- Не логировать секреты (SSH-креды, приватные ключи) и ресурсы, которые посещают пользователи VPN.
- Строки интерфейса — через gettext (RU и EN). Исходная строка — на английском; выполните
  `make messages` и впишите русский перевод в обновлённый `django.po`. `make messages-check`
  (то же делает CI) падает на непереведённых строках. Нужен GNU gettext.
- Перед push: `ruff check`, `ruff format --check`, `mypy`, `pytest`.

### Документация

Если изменилось поведение или настройки — обновите `docs/wiki/` (`ru/` и `en/`) и
[CHANGELOG.md](CHANGELOG.md) в том же pull request.

### Коммиты и pull request

- [Conventional Commits](https://www.conventionalcommits.org/ru/): `feat(keys): …`, `fix(ssh): …`,
  `docs: …`. Сообщения короткие.
- Один pull request — одна тема. Заполните шаблон pull request.
- Pull request — в ветку `dev`, в ней идёт разработка. В ветке `releases` только релизы.

### Релизы

Версии — по [Semantic Versioning](https://semver.org/lang/ru/): `MAJOR.MINOR.PATCH`, предрелизы —
вида `1.0.0-rc.1`. Чтобы выпустить релиз:

1. Указать версию в `pyproject.toml`.
2. В [CHANGELOG.md](CHANGELOG.md) переименовать `[Unreleased]` в `[X.Y.Z] - ГГГГ-ММ-ДД` и добавить над
   ним пустой раздел `[Unreleased]`.
3. Закоммитить в `dev` и дождаться зелёного CI.
4. Передвинуть на него `releases`, поставить тег и отправить:
   `git checkout releases && git merge --ff-only dev && git tag vX.Y.Z &&
   git push origin releases vX.Y.Z`.

Тег публикует образ `ghcr.io/aglamake/lamnezia` (теги `X.Y.Z`, `X.Y`, `X` и `latest`; у предрелиза —
только свой тег) и релиз на GitHub с заметками из раздела changelog. Workflow остановится, если тег
не совпадает с `pyproject.toml` или в changelog нет раздела этой версии.

### Лицензия

Отправляя изменения, вы соглашаетесь распространять их под [AGPL-3.0](LICENSE). Файлы,
перенесённые из `amnezia-client`, сохраняют исходное указание авторства и перечисляются в
[NOTICE](NOTICE).
