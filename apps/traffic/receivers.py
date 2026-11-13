"""Traffic readings for signals of other apps, connected in `TrafficConfig`."""

import logging
from collections.abc import Sequence
from typing import Any

from django.utils import timezone

from amnezia_core.xray import XRAY_CONTAINER
from apps.keys.signals import clients_adding, xray_restarting
from apps.ssh.errors import SshError
from apps.ssh.services import RemoteHost
from apps.traffic.services.poller import TrafficPoller
from apps.traffic.services.recorder import TrafficRecorder

logger = logging.getLogger(__name__)


def on_xray_restarting(sender: object, source: str, host: RemoteHost, **kwargs: Any) -> None:
    """A last reading before the restart zeroes the counters; a failed one only loses it."""
    try:
        TrafficPoller().read(host, source, {source: XRAY_CONTAINER})
    except SshError as error:
        logger.warning("traffic of %s not read before restart: %s", source, error.code)


def on_clients_adding(
    sender: object, source: str, public_keys: Sequence[str], **kwargs: Any
) -> None:
    """New keys count from their first byte: XRay lists a client only once it has traffic."""
    recorder, at = TrafficRecorder(), timezone.now()
    for public_key in public_keys:
        recorder.expect_peer(source, public_key, at)


def connect() -> None:
    xray_restarting.connect(on_xray_restarting, dispatch_uid="traffic.xray_restarting")
    clients_adding.connect(on_clients_adding, dispatch_uid="traffic.clients_adding")
