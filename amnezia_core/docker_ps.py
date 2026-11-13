"""Amnezia containers in `docker ps` output, found as upstream getAlreadyInstalledContainers."""

import re
from dataclasses import dataclass

DOCKER_PS_FORMAT = "{{.Names}} {{.Ports}}"

# Upstream InstallController: a published port, or a bare one for the Tor site and DNS containers.
_PUBLISHED = re.compile(r"(amnezia[-a-z0-9]*).*?:([0-9]*)->[0-9]*/(udp|tcp).*")
_TOR_OR_DNS = re.compile(r"(amnezia-(?:torwebsite|dns)).*?([0-9]*)/(udp|tcp).*")


@dataclass(frozen=True)
class RunningContainer:
    name: str
    port: int | None
    transport: str


def parse_docker_ps(output: str) -> list[RunningContainer]:
    """Running Amnezia containers in the order docker lists them, each name once."""
    found: dict[str, RunningContainer] = {}
    for line in output.splitlines():
        for pattern in (_PUBLISHED, _TOR_OR_DNS):
            match = pattern.search(line)
            if match and match[1] not in found:
                port = int(match[2] or 0) or None
                found[match[1]] = RunningContainer(match[1], port, match[3])
    return list(found.values())
