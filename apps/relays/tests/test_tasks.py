"""The beat tasks of the relays."""

from typing import Any

import pytest
from django.utils import timezone

from apps.relays.models import Relay
from apps.relays.services.monitor import RelayMonitor
from apps.relays.tasks import monitor_relay
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def test_a_reading_is_not_started_over_one_under_way(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = Relay.objects.create(server=ServerFactory(), installed_at=timezone.now())
    polled: list[int] = []

    def slow(self: RelayMonitor, relay: Any) -> None:
        polled.append(relay.pk)
        if len(polled) == 1:
            monitor_relay(relay.pk)

    monkeypatch.setattr(RelayMonitor, "poll", slow)

    monitor_relay(relay.pk)
    monitor_relay(relay.pk)

    assert polled == [relay.pk, relay.pk]
