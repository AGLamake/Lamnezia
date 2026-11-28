"""The kernel OOM messages read before a lost interface is restarted, in a real shell."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from apps.protocols.services.lifecycle import oom_lines

NOW = 1_800_000_000


def _has_uptime() -> bool:
    sh = shutil.which("sh")
    return (
        sh is not None
        and subprocess.run([sh, "-c", "test -r /proc/uptime"], check=False).returncode == 0
    )


pytestmark = pytest.mark.skipif(not _has_uptime(), reason="needs a POSIX shell with /proc")


def run(script: str, env: dict[str, str]) -> str:
    completed = subprocess.run(
        [str(shutil.which("sh")), "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """`date` says NOW; `dmesg` holds an OOM kill 600 s and one 60 s before it."""
    base = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    uptime = float(run("cut -d' ' -f1 /proc/uptime", base))
    lines = [
        f"[{uptime - 600:12.6f}] Out of memory: Killed process 700 (old)",
        f"[{uptime - 60:12.6f}] Out of memory: Killed process 812 (amneziawg-go)",
        f"[{uptime - 30:12.6f}] eth0: link up",
    ]
    dmesg = "".join(f"echo '{line}'\n" for line in lines)
    for name, body in {"date": f"echo {NOW}\n", "dmesg": dmesg}.items():
        (tmp_path / name).write_text(f"#!/bin/sh\n{body}", newline="\n")
        (tmp_path / name).chmod(0o755)
    return base


def test_without_a_moment_every_oom_message_is_read(env: dict[str, str]) -> None:
    printed = run(oom_lines(), env)

    assert "Killed process 700" in printed
    assert "Killed process 812" in printed
    assert "link up" not in printed


def test_with_the_moment_of_the_loss_older_messages_are_left_out(env: dict[str, str]) -> None:
    printed = run(oom_lines(after=NOW - 120), env)

    assert "Killed process 700" not in printed
    assert "Killed process 812 (amneziawg-go)" in printed
