"""Troubles of a deleted server or relay go with it."""

import pytest

from apps.notifications.models import Trouble
from apps.relays.models import Relay
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

MOMENT = "2026-11-16T10:00:00"


def keys() -> set[str]:
    return set(Trouble.objects.values_list("key", flat=True))


def add(*keys: str) -> None:
    for key in keys:
        Trouble.objects.create(key=key)


def test_deleting_a_server_forgets_its_outages() -> None:
    server, other = ServerFactory(), ServerFactory()
    kept = {f"server_down:{other.pk}:never", f"member_lagging:{server.pk}:{MOMENT}"}
    add(*kept, f"server_down:{server.pk}:never", f"awg3_dropped:{server.pk}:{MOMENT}")

    server.delete()

    assert keys() == kept


def relay_whose_pk_is_another_server(server: Server) -> tuple[Relay, Server]:
    """A relay on `server` whose pk equals the pk of another server: the keys must not mix."""
    other = ServerFactory()
    relay = Relay.objects.create(pk=other.pk, server=server)
    assert relay.pk != server.pk
    return relay, other


def test_deleting_a_relay_server_forgets_the_relay_by_its_own_pk() -> None:
    relay, other = relay_whose_pk_is_another_server(ServerFactory())
    kept = {f"server_down:{other.pk}:never", f"awg3_dropped:{other.pk}:{MOMENT}"}
    add(*kept, f"relay_down:{relay.pk}:never", f"relay_settings_differ:{relay.server_id}:{MOMENT}")

    relay.server.delete()

    assert keys() == kept


def test_deleting_a_server_keeps_the_outage_of_a_relay_with_its_pk() -> None:
    relay, other = relay_whose_pk_is_another_server(ServerFactory())
    kept = {f"relay_down:{relay.pk}:never"}
    add(*kept, f"server_down:{other.pk}:never")

    other.delete()

    assert keys() == kept


def test_removing_the_relay_role_forgets_its_troubles_and_keeps_the_server() -> None:
    relay, _other = relay_whose_pk_is_another_server(ServerFactory())
    kept = {f"server_down:{relay.server_id}:never"}
    add(*kept, f"relay_down:{relay.pk}:never", f"relay_settings_differ:{relay.server_id}:{MOMENT}")

    relay.delete()

    assert keys() == kept
