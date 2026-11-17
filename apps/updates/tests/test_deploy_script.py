"""lamnezia-deploy.sh release against a local git origin, with fake docker and curl."""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from django.conf import settings

SCRIPT = settings.BASE_DIR / "deploy" / "update" / "lamnezia-deploy.sh"
RUN = "0123456789abcdef0123456789abcdef"
DIGEST = "sha256:" + "a" * 64
REPOSITORY = "ghcr.io/example/lamnezia"

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
    any(shutil.which(tool) is None for tool in ("sh", "git")), reason="no sh or git"
)


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

    def manifest(self, tag: str, commit: str, image: str = DIGEST) -> None:
        folder = self.root / "releases" / tag
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "lamnezia-release.txt").write_text(f"commit {commit}\nimage {image}\n")

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
    (tmp_path / "settings").write_text(
        f"HEALTH_URL=http://health\nHEALTH_TRIES=1\nHEALTH_DELAY=0\nRELEASES_URL={releases_url}\n",
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

    assert host.status()["reason"] == "unverified"
    assert host.version() == "1.0.0"
    assert not (host.root / "docker.log").exists()


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
