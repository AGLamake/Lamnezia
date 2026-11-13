# Обновление

## Перед обновлением

1. Прочитайте, что изменилось, в [CHANGELOG.md](../../../CHANGELOG.md): несовместимые изменения и
   новые настройки перечислены там.
2. Сделайте бэкап ([Бэкапы](Backup.md)):

   ```bash
   cd /opt/lamnezia
   docker compose run --rm backup sh /usr/local/bin/backup.sh once
   ```

## Обновление

```bash
cd /opt/lamnezia
git pull
docker compose up -d --build
```

Или запустите установщик ещё раз: он обновит код, а `.env`, сайт nginx и данные оставит как есть.

`git pull` идёт по ветке установки: в `releases` только релизы (её берёт установщик), `dev` —
ветка разработки. Установка, сделанная до 1.0, стоит на `main`, которой больше нет; один раз
переведите её на релизы:

```bash
git fetch origin releases
git checkout -B releases origin/releases
```

Миграции базы выполняются сами при каждом запуске `web`. VPN-серверы работают, пока панель
перезапускается; фоновые операции, которые шли в этот момент, через `LAMNEZIA_OPERATION_TIMEOUT`
помечаются прерванными, их можно повторить.

Новые необязательные настройки получают значения по умолчанию; сравните свой `.env` с
`.env.example`, чтобы их увидеть ([Настройка](Configuration.md)).

## Автоматическое обновление из GitHub

Workflow **Deploy** обновляет панель после каждого зелёного CI в `dev` (его можно запустить и
вручную). Он подключается по SSH своим ключом, которому разрешён только скрипт
[`deploy/update/lamnezia-deploy.sh`](../../../deploy/update/lamnezia-deploy.sh): скрипт получает
`dev` (`BRANCH` в его настройках; checkout на сервере должен быть на этой ветке) с временным
токеном задания (на сервере он не сохраняется), переходит на проверенный коммит (запоздавший запуск никогда не откатывает версию), выполняет `docker compose up -d --build` и ждёт
ответа `/healthz`. Если задан `LAMNEZIA_IMAGE` (в `.env` или `/etc/default/lamnezia-deploy`), скрипт
ничего не собирает: вместо этого он выполняет `docker compose pull` и `docker compose up -d --no-build`.
После обновления скрипт удаляет старые образы панели, чтобы они не копили место на диске: после
сборки — образы проекта без тега и кэш сборки старше недели, после `pull` — предыдущую версию образа.
Без секрета `DEPLOY_HOST` workflow ничего не делает.

Настройка, один раз:

1. На сервере установите скрипт и его настройки:

   ```bash
   install -m 755 deploy/update/lamnezia-deploy.sh /usr/local/sbin/lamnezia-deploy
   cat > /etc/default/lamnezia-deploy <<'EOF'
   LAMNEZIA_DIR=/opt/lamnezia
   COMPOSE_PROJECT=lamnezia
   HEALTH_URL=https://panel.example.com/healthz
   EOF
   ```

2. Создайте отдельную пару ключей для обновления (`ssh-keygen -t ed25519 -N "" -f lamnezia-deploy`)
   и добавьте открытый ключ в `~/.ssh/authorized_keys` сервера с ограничением на скрипт:

   ```text
   restrict,command="/usr/local/sbin/lamnezia-deploy" ssh-ed25519 AAAA... lamnezia-deploy
   ```

3. В настройках репозитория (**Secrets and variables → Actions**) добавьте `DEPLOY_HOST`,
   `DEPLOY_SSH_KEY` (закрытый ключ), `DEPLOY_KNOWN_HOSTS` (строка из `ssh-keyscan -t ed25519
   <хост>`), при необходимости `DEPLOY_PORT` (22) и `DEPLOY_USER` (root). После этого удалите закрытый
   ключ со своего компьютера.

Сервер запускает установленную копию скрипта: после изменения `deploy/update/lamnezia-deploy.sh`
установите его заново. Чтобы остановить обновления, удалите строку ключа из `authorized_keys` или
секрет `DEPLOY_HOST`.

## Релизы

Релизы помечаются тегами `vX.Y.Z` ([Semantic Versioning](https://semver.org/lang/ru/)). Чтобы
остаться на определённом релизе, а не на последнем из `releases`:

```bash
git fetch --tags
git checkout vX.Y.Z
docker compose up -d --build
```

С каждым релизом публикуется и готовый образ `ghcr.io/aglamake/lamnezia:X.Y.Z` (и теги `X.Y`, `X`,
`latest`) для `amd64` и `arm64`. Если в `.env` задан `LAMNEZIA_IMAGE`
([готовый образ](Installation.md#готовый-образ-вместо-сборки)), поменяйте в нём версию и выполните
`docker compose pull && docker compose up -d --no-build`.

## Проверка после обновления

- `docker compose ps` показывает `healthy` у `web`, `worker`, `postgres` и `redis`;
- `https://panel.example.com/healthz` отвечает `{"status": "ok"}`;
- в **Операциях** нет новых упавших операций.

## Откат

Миграции автоматически не откатываются. Чтобы вернуться на прошлую версию, восстановите бэкап,
сделанный перед обновлением ([Бэкапы](Backup.md#восстановление-панели)), и переключитесь на прошлый тег.
