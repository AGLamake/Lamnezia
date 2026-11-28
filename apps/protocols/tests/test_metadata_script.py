"""The script of the cloud metadata block in a real shell, on both firewall backends of Docker."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from apps.protocols.services import metadata_scripts as scripts
from apps.protocols.services.metadata_scripts import MetadataReading

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")

# Each fake keeps its rules in a file of $STATE, one rule a line, as the real tool lists them.
_DOCKER = """#!/bin/sh
case $5 in
  bridge) echo "0a1b2c3d4e5f6a7b docker0" ;;
  amnezia-dns-net) echo "1a2b3c4d5e6f7a8b <no value>" ;;
  *) exit 1 ;;
esac
"""

_IPTABLES = """#!/bin/sh
rules="$STATE/$(basename "$0")"
[ -n "$DOCKER_USER" ] || exit 1
touch "$rules"
case $1 in
  -n) [ "$3" = DOCKER-USER ] || grep -qx -- "-N $3" "$rules" ;;
  -N) echo "-N $2" >> "$rules" ;;
  -C) shift; grep -qx -- "-A $*" "$rules" ;;
  -A|-I) shift; echo "-A $*" >> "$rules" ;;
  -S) grep -- " $2 \\|-N $2" "$rules" | grep -v "^-A DOCKER-USER" ;;
  *) exit 1 ;;
esac
"""

_NFT = """#!/bin/sh
rules="$STATE/nft"
case "$1 $2 $3" in
  "list table ip") [ -n "$DOCKER_NFT" ] ;;
  "list chain inet") [ -f "$rules" ] && cat "$rules" ;;
  "add table inet") ;;
  "add chain inet") echo "type filter hook forward priority -1; policy accept;" > "$rules" ;;
  "add rule inet") shift 5; printf '\\t\\t%s "%s" %s %s %s %s\\n' "$@" >> "$rules" ;;
  *) exit 1 ;;
esac
"""

_SYSTEMCTL = "#!/bin/sh\necho active\n"


@pytest.fixture
def host(tmp_path: Path) -> dict[str, str]:
    tools = tmp_path / "bin"
    tools.mkdir()
    for name, body in {
        "docker": _DOCKER,
        "iptables": _IPTABLES,
        "ip6tables": _IPTABLES,
        "nft": _NFT,
        "systemctl": _SYSTEMCTL,
    }.items():
        (tools / name).write_text(body, newline="\n")
        (tools / name).chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    return {**os.environ, "STATE": str(state), "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"}


def run(script: str, env: dict[str, str]) -> str:
    sh = str(shutil.which("sh"))
    completed = subprocess.run(
        [sh, "-c", script], check=False, env=env, capture_output=True, text=True
    )
    return completed.stdout


def test_iptables_closes_both_bridges_for_ipv4_and_ipv6(host: dict[str, str]) -> None:
    env = {**host, "DOCKER_USER": "1"}

    printed = run(scripts.SCRIPT, env)

    assert printed.splitlines() == [
        "docker0: 169.254.0.0/16 closed",
        "br-1a2b3c4d5e6f: 169.254.0.0/16 closed",
        "iptables DOCKER-USER: LAMNEZIA-METADATA added",
        "docker0: fd00:ec2::254 closed",
        "br-1a2b3c4d5e6f: fd00:ec2::254 closed",
        "ip6tables DOCKER-USER: LAMNEZIA-METADATA added",
    ]
    assert run(scripts.SCRIPT, env) == ""
    bridges = ("docker0", "br-1a2b3c4d5e6f")
    assert MetadataReading.parse(run(scripts.STATUS, env)) == MetadataReading(
        True, True, bridges, bridges, "iptables"
    )


def test_nftables_backend_gets_a_table_of_its_own(host: dict[str, str]) -> None:
    """Docker with nftables has no DOCKER-USER: a drop in another forward chain is final."""
    env = {**host, "DOCKER_NFT": "1"}

    printed = run(scripts.SCRIPT, env)

    assert printed.splitlines() == [
        "nftables: lamnezia_metadata added",
        "docker0: 169.254.0.0/16 closed",
        "docker0: fd00:ec2::254 closed",
        "br-1a2b3c4d5e6f: 169.254.0.0/16 closed",
        "br-1a2b3c4d5e6f: fd00:ec2::254 closed",
    ]
    assert run(scripts.SCRIPT, env) == ""
    bridges = ("docker0", "br-1a2b3c4d5e6f")
    assert MetadataReading.parse(run(scripts.STATUS, env)) == MetadataReading(
        True, True, bridges, bridges, "nftables"
    )


def test_nothing_is_added_before_docker_starts(host: dict[str, str]) -> None:
    assert run(scripts.SCRIPT, host) == ""
    assert not MetadataReading.parse(run(scripts.STATUS, host)).closed


def test_iptables_without_ip6tables_rules_leaves_ipv6_open(host: dict[str, str]) -> None:
    """Docker with `ip6tables: false` makes no DOCKER-USER for IPv6."""
    env = {**host, "DOCKER_USER": "1"}
    tools = Path(env["PATH"].split(os.pathsep)[0])
    (tools / "ip6tables").write_text("#!/bin/sh\nexit 1\n", newline="\n")

    run(scripts.SCRIPT, env)
    reading = MetadataReading.parse(run(scripts.STATUS, env))

    assert reading.closed
    assert reading.ipv6_bridges == ()
    assert reading.backend == "iptables"
