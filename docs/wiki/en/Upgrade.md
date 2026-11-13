# Upgrade

## Before updating

1. Read what changed in [CHANGELOG.md](../../../CHANGELOG.md): breaking changes and new settings are
   listed there.
2. Make a backup ([Backup](Backup.md)):

   ```bash
   cd /opt/lamnezia
   docker compose run --rm backup sh /usr/local/bin/backup.sh once
   ```

## Updating

```bash
cd /opt/lamnezia
git pull
docker compose up -d --build
```

Or run the installer again: it updates the code and keeps `.env`, the nginx site and the data.

`git pull` follows the branch of the installation: `releases` holds only releases (the
installer takes it), `dev` is the development branch. An installation made before 1.0 is on
`main`, which is gone; move it to the releases once:

```bash
git fetch origin releases
git checkout -B releases origin/releases
```

Database migrations run by themselves on every `web` start. VPN servers keep working while the panel
restarts; background operations that were running are marked interrupted after
`LAMNEZIA_OPERATION_TIMEOUT` and can be retried.

New optional settings get their defaults; compare your `.env` with `.env.example` to see them
([Configuration](Configuration.md)).

## Automatic update from GitHub

The **Deploy** workflow updates the panel after every green CI on `dev` (it can also be run by
hand). It connects over SSH with its own key, which may run only
[`deploy/update/lamnezia-deploy.sh`](../../../deploy/update/lamnezia-deploy.sh): the script fetches
`dev` (`BRANCH` in its settings; the checkout on the server must be on it) with the short-lived
token of the job (never stored on the server), moves to the tested commit (a late run never rolls back), runs `docker compose up -d --build` and waits for `/healthz`.
With `LAMNEZIA_IMAGE` set (in `.env` or `/etc/default/lamnezia-deploy`) it builds nothing: it runs
`docker compose pull` and `docker compose up -d --no-build` instead. After the update the script
removes old panel images so they do not fill the disk: after a build, the untagged images of the
project and build cache older than a week; after a pull, the previous version of the image.
Without the `DEPLOY_HOST` secret the workflow does nothing.

Setup, once:

1. On the server, install the script and its settings:

   ```bash
   install -m 755 deploy/update/lamnezia-deploy.sh /usr/local/sbin/lamnezia-deploy
   cat > /etc/default/lamnezia-deploy <<'EOF'
   LAMNEZIA_DIR=/opt/lamnezia
   COMPOSE_PROJECT=lamnezia
   HEALTH_URL=https://panel.example.com/healthz
   EOF
   ```

2. Make a key pair just for the update (`ssh-keygen -t ed25519 -N "" -f lamnezia-deploy`) and add the
   public key to `~/.ssh/authorized_keys` of the server limited to the script:

   ```text
   restrict,command="/usr/local/sbin/lamnezia-deploy" ssh-ed25519 AAAA... lamnezia-deploy
   ```

3. In the repository settings (**Secrets and variables → Actions**) add `DEPLOY_HOST`,
   `DEPLOY_SSH_KEY` (the private key), `DEPLOY_KNOWN_HOSTS` (the line from `ssh-keyscan -t ed25519
   <host>`), and if needed `DEPLOY_PORT` (22) and `DEPLOY_USER` (root). Then delete the private key
   from your computer.

The server runs its installed copy of the script: after a change of
`deploy/update/lamnezia-deploy.sh` install it again. To stop the updates, remove the key line from
`authorized_keys` or the `DEPLOY_HOST` secret.

## Releases

Releases are tagged `vX.Y.Z` ([Semantic Versioning](https://semver.org/)). To stay on a particular
release instead of the tip of `releases`:

```bash
git fetch --tags
git checkout vX.Y.Z
docker compose up -d --build
```

Each release also publishes a ready image `ghcr.io/aglamake/lamnezia:X.Y.Z` (and the `X.Y`, `X`,
`latest` tags) for `amd64` and `arm64`. With `LAMNEZIA_IMAGE` in `.env`
([ready image](Installation.md#ready-image-instead-of-a-build)), change the version there and run
`docker compose pull && docker compose up -d --no-build`.

## Checks after updating

- `docker compose ps` shows `healthy` for `web`, `worker`, `postgres` and `redis`;
- `https://panel.example.com/healthz` returns `{"status": "ok"}`;
- **Operations** shows no new failed operations.

## Rolling back

Migrations are not rolled back automatically. To return to the previous version, restore the backup
made before the update ([Backup](Backup.md#restoring-the-panel)) and check out the previous tag.
