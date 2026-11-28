"""The relay image and agent the panel ships: their sources, the image version, the agent digest.

Split routing (docs/SPLIT.md) needs what an older image lacks (iproute2, unbound, the loose
reverse path check of `docker run`): Update only brings the agent, so a relay started from an
older image needs a reinstall. The panel keeps the image version a relay was installed with and
the digest of the agent it last wrote, and compares them with these.
"""

import hashlib
from pathlib import Path

# Raise it when the Dockerfile or the `docker run` arguments of the relay change.
# 2: iproute2, unbound and the CA certificates in the image, rp_filter=2 on the container.
IMAGE_VERSION = 2

_HERE = Path(__file__).resolve().parent.parent


def dockerfile() -> str:
    return (_HERE / "container" / "Dockerfile").read_text(encoding="utf-8")


def agent_source() -> bytes:
    return (_HERE / "agent" / "relay_agent.py").read_bytes()


def agent_digest() -> str:
    """SHA-256 of the agent of this release."""
    return hashlib.sha256(agent_source()).hexdigest()
