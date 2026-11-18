"""Troubles of a deleted server or relay go with it."""

import pytest

from apps.notifications.models import Trouble
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def test_deleting_a_server_forgets_its_outages() -> None:
    server, other = ServerFactory(), ServerFactory()
    kept = {f"server_down:{other.pk}:never", f"member_lagging:{server.pk}:2026-11-16T10:00:00"}
    gone = {
        f"server_down:{server.pk}:never",
        f"relay_down:{server.pk}:2026-11-16T10:00:00",
        f"awg3_dropped:{server.pk}:2026-11-16T10:00:00",
    }
    for key in kept | gone:
        Trouble.objects.create(key=key)

    server.delete()

    assert set(Trouble.objects.values_list("key", flat=True)) == kept
