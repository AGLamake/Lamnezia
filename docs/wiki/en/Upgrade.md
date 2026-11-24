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
installer takes it), `dev` is the development branch. The installer keeps the code on a local
branch that tracks the same branch of `origin`, so `git pull` works after the installer runs again
too. An installation on a tag (`LAMNEZIA_REF=vX.Y.Z`) stays on that version and `git pull` does not
work on it — see "Releases".

An installation made before 1.0 is on `main`, which is gone, and running an older installer again
left the code on no branch (`git pull` answers "You are not currently on a branch"). Move such an
installation to the releases once:

```bash
git fetch origin releases
git checkout -B releases origin/releases
```

Database migrations run by themselves on every `web` start. VPN servers keep working while the panel
restarts; background operations that were running are marked interrupted after
`LAMNEZIA_OPERATION_TIMEOUT` and can be retried. An update from 1.0.0 needs nothing but these
migrations: the moderator of each user becomes their only staff member, and every moderator keeps
their users, servers, quotas and requests; the access of administrators and moderators stays
the same ([Roles and access](Roles-and-access.md#update-from-100)).

New optional settings get their defaults; compare your `.env` with `.env.example` to see them
([Configuration](Configuration.md)).

## Update from the panel

Twice a day the panel asks GitHub for the latest release (its tag and text; nothing about the panel
is sent). When a newer one is out, administrators see it on the dashboard and on **System → Panel
update**, with links to the release and its CHANGELOG. The page also shows the text of the release
as Markdown, with the HTML written in it removed; a long text is folded and opens on a click. `LAMNEZIA_UPDATE_CHECK=False` turns the check
off ([Configuration](Configuration.md)).

On that page an administrator starts the update with a password for the panel archive:

1. The panel makes an archive ([Panel archive](Backup.md#panel-archive)) with that password.
2. It leaves a request in `update/request` of the install folder. The panel has no access to Docker
   or to the host: the `lamnezia-update.path` unit on the host sees the request and runs
   `/usr/local/sbin/lamnezia-deploy release`.
3. The host script takes only a release tag `vX.Y.Z` of the branch the installation follows, newer
   than the running version, and checks it against the manifest published with the GitHub release
   (the version, the tagged commit and, with `LAMNEZIA_IMAGE`, the image digest). The manifest must
   carry a valid signature by a key of `deploy/update/release-signers` of the running version;
   without it the update stops before anything changes, with the reason on the page. It moves the
   code to the tag, rebuilds or pulls the image, restarts the panel and waits for `/healthz`.
4. If the panel does not come up healthy, the script goes back to the previous version.

The status and the log of the host script are on the same page. One update runs at a time; a request
the host has not taken for an hour is shown as stuck. The panel does not
answer for a minute or two while it restarts.

Database migrations are not taken back on a rollback. If the previous version does not work with the
migrated database, restore the archive made before the update ([Backup](Backup.md#panel-archive)).

The installer sets up the unit. An installation made before it shows the new release without the
**Update** button: run `install.sh` of the current version once, or update by hand as above. Only a
git checkout can be updated from the panel. Check the unit with
`systemctl status lamnezia-update.path`; the script log of the last run is in
`update/status/log`.

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

### Checking that a release is genuine

The release workflow signs where the image and the release files come from (a build provenance
attestation by GitHub), and the image also gets a signed SBOM, the list of its packages. Besides
`lamnezia-release.txt`, a release carries `lamnezia-X.Y.Z.tar.gz`, the code of the tag. The
[GitHub CLI](https://cli.github.com/) checks them; a check that passes says the file was built by
the release workflow of this repository:

```bash
gh attestation verify oci://ghcr.io/aglamake/lamnezia:X.Y.Z --repo AGLamake/Lamnezia
gh release download vX.Y.Z --repo AGLamake/Lamnezia
gh attestation verify lamnezia-X.Y.Z.tar.gz --repo AGLamake/Lamnezia
gh attestation verify lamnezia-release.txt --repo AGLamake/Lamnezia
```

The SBOM of the image is checked with
`--predicate-type https://spdx.dev/Document/v2.3` added to the first command. Releases before
this one have no attestations.

The manifest is also signed with the release SSH key (`lamnezia-release.txt.sig`). The update
from the panel checks this signature itself; by hand, in the install folder (OpenSSH 8.1 or newer):

```bash
ssh-keygen -Y verify -f deploy/update/release-signers -I lamnezia-release -n lamnezia-release \
  -s lamnezia-release.txt.sig < lamnezia-release.txt
```

## Checks after updating

- `docker compose ps` shows `healthy` for `web`, `worker`, `postgres` and `redis`;
- `https://panel.example.com/healthz` returns `{"status": "ok"}`;
- **Operations** shows no new failed operations.

## Rolling back

Migrations are not rolled back automatically. To return to the previous version, restore the backup
made before the update ([Backup](Backup.md#restoring-the-panel)) and check out the previous tag.
