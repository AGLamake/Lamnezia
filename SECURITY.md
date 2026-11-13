# Security Policy

[English](#english) · [Русский](#русский)

## English

### Supported versions

Lamnezia is in early development. Security fixes are made only for the latest release and the
`main` branch.

### Reporting a vulnerability

Please **do not** open a public issue. Report privately through GitHub:
**Security → Report a vulnerability** in this repository
([private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)).

Include the affected version or commit, steps to reproduce and the possible impact. Do not include
real SSH credentials, private keys or `vpn://` keys — use test data.

We aim to acknowledge a report within 7 days and to agree on a disclosure date with you.

### Hardening

How to run the panel safely (roles, two-factor login, encryption of secrets and changing its key,
a separate SSH key for the panel, a firewall for the panel host, a checklist):
[docs/wiki/en/Security.md](docs/wiki/en/Security.md).

### Scope

In scope: the Lamnezia panel, its background tasks and installers in this repository.
Vulnerabilities in the Amnezia app or the AmneziaWG protocol should be reported to the
[Amnezia VPN project](https://github.com/amnezia-vpn).

## Русский

### Поддерживаемые версии

Lamnezia на ранней стадии разработки. Исправления безопасности выходят только для последнего
релиза и ветки `main`.

### Как сообщить об уязвимости

**Не открывайте** публичный issue. Сообщите приватно через GitHub:
**Security → Report a vulnerability** в этом репозитории.

Укажите версию или коммит, шаги воспроизведения и возможные последствия. Не прикладывайте
настоящие SSH-креды, приватные ключи и ключи `vpn://` — только тестовые данные.

Постараемся ответить в течение 7 дней и согласовать с вами дату раскрытия.

### Защита установки

Как безопасно развернуть панель (роли, двухфакторный вход, шифрование секретов и смена ключа,
отдельный SSH-ключ панели, firewall хоста панели, чек-лист):
[docs/wiki/ru/Security.md](docs/wiki/ru/Security.md).

### Область

Входит: панель Lamnezia, её фоновые задачи и установщики из этого репозитория.
Об уязвимостях в приложении Amnezia и протоколе AmneziaWG сообщайте
[проекту Amnezia VPN](https://github.com/amnezia-vpn).
