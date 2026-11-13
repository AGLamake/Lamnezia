from datetime import timedelta

import pytest
from django.utils import timezone

from amnezia_core.xray import client_fingerprint
from apps.keys.signals import clients_adding, xray_restarting
from apps.ssh.errors import ErrorCode, SshCommandError
from apps.ssh.services import CommandResult, RemoteHost
from apps.ssh.tests.fakes import TIMEOUTS, FakeRunner, result
from apps.traffic.models import TrafficCounter, TrafficHourly
from apps.traffic.services.poller import TrafficPoller
from apps.traffic.tests.test_poller import marker_output

pytestmark = pytest.mark.django_db

ALICE = client_fingerprint("6f0c3e52-1b7a-4c8e-9d2f-0a1b2c3d4e5f")
STATS = (
    f'{{"stat": [{{"name": "user>>>{ALICE}>>>traffic>>>uplink", "value": 10}},'
    f' {{"name": "user>>>{ALICE}>>>traffic>>>downlink", "value": 20}}]}}'
)


def test_counters_are_read_before_the_restart() -> None:
    runner = FakeRunner({"statsquery": result(stdout=marker_output(("amnezia-xray", 0, STATS)))})

    xray_restarting.send(object, source="xray:1", host=RemoteHost(runner, TIMEOUTS))

    counter = TrafficCounter.objects.get(source="xray:1")
    assert (counter.public_key, counter.rx_bytes, counter.tx_bytes) == (ALICE, 10, 20)


class DroppingRunner(FakeRunner):
    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        if "statsquery" in command:
            raise SshCommandError(ErrorCode.COMMAND_TIMEOUT)
        return super().run(command, stdin=stdin, timeout=timeout)


def test_server_that_drops_the_reading_does_not_fail_the_sender() -> None:
    host = RemoteHost(DroppingRunner(), TIMEOUTS)

    xray_restarting.send(object, source="xray:1", host=host)

    assert not TrafficCounter.objects.exists()


def test_new_xray_key_counts_its_first_reading() -> None:
    """XRay lists a client only after its first bytes: they are traffic, not a baseline."""
    clients_adding.send(object, source="xray:1", public_keys=[ALICE])
    runner = FakeRunner({"statsquery": result(stdout=marker_output(("amnezia-xray", 0, STATS)))})
    # A reading is always taken after the key is added; with a coarse clock (Windows) two
    # `now()` in a row can be equal, and a reading no newer than the counter is dropped.
    later = timezone.now() + timedelta(seconds=1)

    TrafficPoller(clock=lambda: later).read(
        RemoteHost(runner, TIMEOUTS), "xray:1", {"xray:1": "amnezia-xray"}
    )

    row = TrafficHourly.objects.get(source="xray:1", public_key=ALICE)
    assert (row.rx_bytes, row.tx_bytes) == (10, 20)
