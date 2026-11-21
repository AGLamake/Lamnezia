"""install.sh timers and units on a host with systemd and without it, with a fake systemctl."""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from django.conf import settings

SCRIPT = settings.BASE_DIR / "install.sh"
FAKE_SYSTEMCTL = '#!/bin/sh\necho "$*" >> "$FAKE_LOG"\n'
# Sources the functions, points them at the test folders and installs what a move target gets.
STEPS = """
LAMNEZIA_INSTALL_FUNCTIONS_ONLY=1 source "$T_SCRIPT"
DIR=$T_DIR SYSTEMD_DIR=$T_UNITS SYSTEMD_RUN=$T_RUN DEPLOY_SCRIPT=$T_DEPLOY DOMAIN=panel.example.com
retry_certificate
install_stop_timer
install_update_unit
warn_no_systemd
"""


def find_bash() -> str | None:
    """The bash next to sh: on Windows the one on PATH may be WSL's."""
    sh = shutil.which("sh")
    if sh:
        for name in ("bash", "bash.exe"):
            beside = Path(sh).with_name(name)
            if beside.exists():
                return str(beside)
    return shutil.which("bash")


BASH = find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no bash")


@dataclass
class Host:
    root: Path

    @property
    def units(self) -> Path:
        return self.root / "units"

    @property
    def log(self) -> Path:
        return self.root / "systemctl.log"

    def install(self) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "PATH": f"{(self.root / 'bin').as_posix()}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": self.log.as_posix(),
            "T_SCRIPT": SCRIPT.as_posix(),
            "T_DIR": (self.root / "panel").as_posix(),
            "T_UNITS": self.units.as_posix(),
            "T_RUN": (self.root / "run-systemd").as_posix(),
            "T_DEPLOY": (self.root / "lamnezia-deploy").as_posix(),
        }
        command = [BASH or "bash", "-c", STEPS]
        return subprocess.run(command, env=environment, capture_output=True, text=True, check=False)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


@pytest.fixture
def host(tmp_path: Path) -> Host:
    """A git checkout of the panel and a fake systemctl on PATH; each test decides on systemd."""
    for folder in ("bin", "units", "panel/.git", "panel/deploy/update", "panel/update/status"):
        (tmp_path / folder).mkdir(parents=True)
    (tmp_path / "panel" / "deploy" / "update" / "lamnezia-deploy.sh").write_text("#!/bin/sh\n")
    systemctl = tmp_path / "bin" / "systemctl"
    systemctl.write_text(FAKE_SYSTEMCTL, newline="\n")
    systemctl.chmod(0o755)
    return Host(tmp_path)


def test_with_systemd_the_timers_are_installed_and_started(host: Host) -> None:
    (host.root / "run-systemd").mkdir()

    done = host.install()

    assert done.returncode == 0, done.stderr
    assert "enable --now lamnezia-certificate.timer" in host.calls()
    assert "enable --now lamnezia-stop.timer" in host.calls()
    assert "enable --now lamnezia-update.path" in host.calls()
    deploy = (host.root / "lamnezia-deploy").as_posix()
    assert f"ExecStart={deploy} release" in (host.units / "lamnezia-update.service").read_text()
    assert (host.units / "lamnezia-stop.timer").exists()
    assert "no systemd" not in done.stderr


def test_without_systemd_the_install_goes_on_and_lists_what_does_not_work(host: Host) -> None:
    done = host.install()

    assert done.returncode == 0, done.stderr
    assert host.calls() == []
    assert list(host.units.iterdir()) == []
    assert not (host.root / "lamnezia-deploy").exists()
    assert "this host has no systemd" in done.stderr
    for feature in ("certificate retry", "stop of this copy", "updates from the Update page"):
        assert feature in done.stderr
    assert "certbot --nginx -d panel.example.com" in done.stderr
    assert "git pull && docker compose up -d --build" in done.stderr
