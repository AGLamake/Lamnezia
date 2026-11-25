"""lamnezia-deploy.sh release against a local git origin, fake docker and curl, real signatures."""

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from django.conf import settings

SCRIPT = settings.BASE_DIR / "deploy" / "update" / "lamnezia-deploy.sh"
RUN = "0123456789abcdef0123456789abcdef"
DIGEST = "sha256:" + "a" * 64
REPOSITORY = "ghcr.io/example/lamnezia"
NAMESPACE = "lamnezia-release"

FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
[ "$1 $2" = "image inspect" ] && echo "$FAKE_REPO_DIGEST"
exit 0
"""
# The health URL fails while the checkout holds FAKE_BROKEN_VERSION; file:// serves the manifests.
FAKE_CURL = """#!/bin/sh
for arg; do url=$arg; done
case "$url" in
    http://health*) [ "$(cat "$LAMNEZIA_DIR/version")" != "${FAKE_BROKEN_VERSION:-}" ] ;;
    file://*) cat "${url#file://}" 2>/dev/null || exit 22 ;;
    *) exit 6 ;;
esac
"""
FAKE_FLOCK = "#!/bin/sh\nexit 0\n"

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("sh", "git", "ssh-keygen")),
    reason="no sh, git or ssh-keygen",
)


def ssh_keygen(*args: str) -> None:
    subprocess.run(["ssh-keygen", "-q", *args], capture_output=True, check=True)


def signing_key(folder: Path, name: str) -> Path:
    """A new ed25519 key pair; the private key's path."""
    folder.mkdir(exist_ok=True)
    key = folder / name
    ssh_keygen("-t", "ed25519", "-N", "", "-C", name, "-f", key.as_posix())
    return key


def git(cwd: Path, *args: str) -> str:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=environment, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@dataclass
class Host:
    root: Path
    panel: Path
    commits: dict[str, str]

    @property
    def update(self) -> Path:
        return self.root / "update"

    @property
    def key(self) -> Path:
        return self.root / "keys" / "release"

    def manifest(
        self,
        tag: str,
        commit: str,
        image: str = DIGEST,
        key: Path | None = None,
        named: str = "",
    ) -> None:
        """The manifest of release `tag` naming version `named` (the tag), signed by `key`."""
        folder = self.root / "releases" / tag
        folder.mkdir(parents=True, exist_ok=True)
        manifest = folder / "lamnezia-release.txt"
        manifest.write_text(
            f"version {named or tag}\ncommit {commit}\nimage {image}\n", newline="\n"
        )
        manifest.with_suffix(".txt.sig").unlink(missing_ok=True)
        ssh_keygen("-Y", "sign", "-f", (key or self.key).as_posix(), "-n", NAMESPACE, str(manifest))

    def run(self, request: str, **env: str) -> subprocess.CompletedProcess[str]:
        (self.update / "request" / "update").write_text(request + "\n", newline="\n")
        environment = {
            **os.environ,
            "PATH": f"{(self.root / 'bin').as_posix()}{os.pathsep}{os.environ['PATH']}",
            "LAMNEZIA_DIR": self.panel.as_posix(),
            "UPDATE_DIR": self.update.as_posix(),
            "LOCK_FILE": (self.root / "lock").as_posix(),
            "DEPLOY_SETTINGS": (self.root / "settings").as_posix(),
            "FAKE_LOG": (self.root / "docker.log").as_posix(),
            "FAKE_REPO_DIGEST": f"{REPOSITORY}@{DIGEST}",
            **env,
        }
        command = [shutil.which("sh") or "sh", SCRIPT.as_posix(), "release"]
        return subprocess.run(command, env=environment, capture_output=True, text=True, check=False)

    def status(self) -> dict[str, str]:
        text = (self.update / "status" / "status").read_text()
        return dict(line.split("=", 1) for line in text.splitlines())

    def version(self) -> str:
        return (self.panel / "version").read_text().strip()

    def log(self) -> str:
        return (self.update / "status" / "log").read_text()

    def assert_refused(self, reason: str) -> None:
        """The update stopped on `reason` before anything changed."""
        assert (self.status()["state"], self.status()["reason"]) == ("failed", reason), self.log()
        assert self.version() == "1.0.0"
        assert not (self.root / "docker.log").exists()


def commit(repo: Path, version: str) -> str:
    (repo / "version").write_text(version + "\n")
    git(repo, "add", "version")
    git(repo, "commit", "-q", "-m", version)
    git(repo, "tag", f"v{version}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def host(tmp_path: Path) -> Host:
    """Origin with v1.0.0 and v1.1.0 on releases; the panel checkout runs v1.0.0."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "releases")
    commits = {version: commit(work, version) for version in ("1.0.0", "1.1.0")}
    git(work, "checkout", "-q", "-b", "other")
    commits["9.0.0"] = commit(work, "9.0.0")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", work.as_posix(), origin.as_posix())
    panel = tmp_path / "panel"
    git(tmp_path, "clone", "-q", "-b", "releases", origin.as_posix(), panel.as_posix())
    git(panel, "reset", "-q", "--hard", "v1.0.0")
    (panel / ".env").write_text("LAMNEZIA_DOMAIN=example.com\n")

    for folder in ("bin", "update/request", "update/status"):
        (tmp_path / folder).mkdir(parents=True)
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL), ("flock", FAKE_FLOCK)):
        path = tmp_path / "bin" / name
        path.write_text(body, newline="\n")
        path.chmod(0o755)
    releases_url = "file://" + (tmp_path / "releases").as_posix()
    key = signing_key(tmp_path / "keys", "release")
    signers = tmp_path / "release-signers"
    public = key.with_suffix(".pub").read_text().strip()
    signers.write_text(f'{NAMESPACE} namespaces="{NAMESPACE}" {public}\n', newline="\n")
    (tmp_path / "settings").write_text(
        f"HEALTH_URL=http://health\nHEALTH_TRIES=1\nHEALTH_DELAY=0\nRELEASES_URL={releases_url}\n"
        f"RELEASE_SIGNERS={signers.as_posix()}\n",
        newline="\n",
    )
    result = Host(tmp_path, panel, commits)
    for version in ("1.0.0", "1.1.0", "9.0.0"):
        result.manifest(f"v{version}", commits[version])
    return result


def test_the_panel_moves_to_the_release(host: Host) -> None:
    done = host.run(f"v1.1.0 {RUN}")

    assert done.returncode == 0, (host.update / "status" / "log").read_text()
    status = host.status()
    assert (status["state"], status["version"], status["previous"]) == ("done", "v1.1.0", "v1.0.0")
    assert status["run"] == RUN
    assert host.version() == "1.1.0"
    assert git(host.panel, "symbolic-ref", "--short", "HEAD") == "releases"
    assert not (host.update / "request" / "update").exists()
    assert (host.update / "status" / "agent").read_text().strip() == "releases"
    assert "compose -p panel build -q" in (host.root / "docker.log").read_text()


def test_a_late_request_never_rolls_back(host: Host) -> None:
    git(host.panel, "merge", "-q", "--ff-only", "v1.1.0")

    done = host.run(f"v1.0.0 {RUN}")

    assert done.returncode != 0
    assert (host.status()["state"], host.status()["reason"]) == ("failed", "not_newer")
    assert host.version() == "1.1.0"


def test_a_release_off_the_channel_is_refused(host: Host) -> None:
    host.run(f"v9.0.0 {RUN}")

    assert host.status()["reason"] == "not_in_channel"
    assert host.version() == "1.0.0"


def test_a_tag_that_does_not_match_its_release_is_refused(host: Host) -> None:
    host.manifest("v1.1.0", host.commits["1.0.0"])

    host.run(f"v1.1.0 {RUN}")

    host.assert_refused("unverified")


def test_a_signed_manifest_of_another_version_is_refused(host: Host) -> None:
    host.manifest("v1.1.0", host.commits["1.1.0"], named="v1.0.1")

    host.run(f"v1.1.0 {RUN}")

    host.assert_refused("unverified")


def test_a_release_without_a_signature_is_refused(host: Host) -> None:
    (host.root / "releases" / "v1.1.0" / "lamnezia-release.txt.sig").unlink()

    host.run(f"v1.1.0 {RUN}")

    host.assert_refused("unsigned")


def test_a_manifest_changed_after_signing_is_refused(host: Host) -> None:
    manifest = host.root / "releases" / "v1.1.0" / "lamnezia-release.txt"
    manifest.write_text(manifest.read_text().replace(DIGEST, "sha256:" + "b" * 64), newline="\n")

    host.run(f"v1.1.0 {RUN}")

    host.assert_refused("bad_signature")


def test_a_manifest_signed_by_another_key_is_refused(host: Host) -> None:
    other = signing_key(host.root / "keys", "other")
    host.manifest("v1.1.0", host.commits["1.1.0"], key=other)

    host.run(f"v1.1.0 {RUN}")

    host.assert_refused("bad_signature")


def test_without_a_trusted_key_nothing_is_updated(host: Host) -> None:
    (host.root / "release-signers").write_text("# no key yet\n", newline="\n")

    host.run(f"v1.1.0 {RUN}")

    host.assert_refused("bad_signature")
    assert "No release signing key" in host.log()


def test_an_unhealthy_release_is_rolled_back(host: Host) -> None:
    done = host.run(f"v1.1.0 {RUN}", FAKE_BROKEN_VERSION="1.1.0")

    assert done.returncode != 0
    assert (host.status()["state"], host.status()["reason"]) == ("rolled_back", "unhealthy")
    assert host.version() == "1.0.0"
    assert "going back to v1.0.0" in (host.update / "status" / "log").read_text()


def test_a_bad_request_changes_nothing(host: Host) -> None:
    host.run("v1.1.0; rm -rf /")

    assert (host.status()["state"], host.status()["reason"]) == ("failed", "bad_request")
    assert host.version() == "1.0.0"
    assert not (host.update / "request" / "update").exists()


def hold_move(host: Host, seconds: int) -> None:
    """The flag the panel keeps while a move operation runs, as services/heartbeat.py writes it."""
    until = int(time.time()) + seconds
    (host.update / "request" / "moving").write_text(f"5 {until}\n", newline="\n")


def test_no_update_while_the_panel_moves(host: Host) -> None:
    hold_move(host, 600)

    done = host.run(f"v1.1.0 {RUN}")

    assert done.returncode != 0
    host.assert_refused("moving")
    assert not (host.update / "request" / "update").exists()


def test_a_flag_left_by_a_dead_worker_does_not_stop_the_update(host: Host) -> None:
    hold_move(host, -1)

    done = host.run(f"v1.1.0 {RUN}")

    assert done.returncode == 0, host.log()
    assert host.version() == "1.1.0"


def test_no_deploy_while_the_panel_moves(host: Host) -> None:
    hold_move(host, 600)
    environment = {
        **os.environ,
        "PATH": f"{(host.root / 'bin').as_posix()}{os.pathsep}{os.environ['PATH']}",
        "LAMNEZIA_DIR": host.panel.as_posix(),
        "UPDATE_DIR": host.update.as_posix(),
        "LOCK_FILE": (host.root / "lock").as_posix(),
        "DEPLOY_SETTINGS": (host.root / "settings").as_posix(),
        "FAKE_LOG": (host.root / "docker.log").as_posix(),
        "SSH_ORIGINAL_COMMAND": f"deploy {host.commits['1.1.0']}",
    }

    done = subprocess.run(
        [shutil.which("sh") or "sh", SCRIPT.as_posix()],
        input=b"token\n",
        env=environment,
        capture_output=True,
        check=False,
    )

    assert done.returncode == 75, done.stderr
    assert b"moving to another machine" in done.stderr
    assert host.version() == "1.0.0"
    assert not (host.root / "docker.log").exists()


def test_a_ready_image_install_pulls_the_checked_image(host: Host) -> None:
    (host.panel / ".env").write_text(f"LAMNEZIA_IMAGE={REPOSITORY}:1.0.0\n")

    done = host.run(f"v1.1.0 {RUN}")

    assert done.returncode == 0, (host.update / "status" / "log").read_text()
    assert (host.panel / ".env").read_text() == f"LAMNEZIA_IMAGE={REPOSITORY}:1.1.0\n"
    log = (host.root / "docker.log").read_text()
    assert f"pull -q {REPOSITORY}:1.1.0" in log
    assert f"image rm {REPOSITORY}:1.0.0" in log


def test_an_image_with_another_digest_is_refused(host: Host) -> None:
    (host.panel / ".env").write_text(f"LAMNEZIA_IMAGE={REPOSITORY}:1.0.0\n")

    host.run(f"v1.1.0 {RUN}", FAKE_REPO_DIGEST=f"{REPOSITORY}@sha256:{'b' * 64}")

    assert host.status()["reason"] == "unverified"
    assert host.version() == "1.0.0"
    assert (host.panel / ".env").read_text() == f"LAMNEZIA_IMAGE={REPOSITORY}:1.0.0\n"


def test_the_release_workflow_signs_what_the_host_checks() -> None:
    workflow = (settings.BASE_DIR / ".github" / "workflows" / "release.yml").read_text()
    script = SCRIPT.read_text()

    assert f"SIGNING_NAMESPACE={NAMESPACE}" in script
    assert "deploy/update/release-signers" in script
    assert f"-n {NAMESPACE}" in workflow and "-f deploy/update/release-signers" in workflow
    assert '"$RUNNER_TEMP/lamnezia-release.txt.sig"' in workflow
