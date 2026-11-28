"""The clone of a split group as a copy of the keys of its leader, for the differences page."""

import pytest

from apps.accounts.models import User
from apps.groups.models import GroupRouting, ServerGroup
from apps.groups.tests.conftest import succeeded
from apps.keys.services.key_copies import key_copies
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import Start
from apps.relays.services import deployment
from apps.relays.services.deployment import INSTALL_KIND
from apps.relays.services.routing import start_routing
from apps.servers.models import Server

pytestmark = pytest.mark.django_db


@pytest.fixture
def installed(relayed: ServerGroup, relay_server: Server, admin: User, start: Start) -> ServerGroup:
    succeeded(start(lambda: deployment.start(INSTALL_KIND, relay_server, user=admin)))
    return relayed


def test_the_leader_of_a_split_group_has_its_clone_as_a_copy(
    installed: ServerGroup,
    leader: ProtocolInstance,
    relay_server: Server,
    admin: User,
    start: Start,
) -> None:
    ip = "198.51.100.7"
    succeeded(start(lambda: start_routing(installed, GroupRouting.SPLIT, user=admin, ip=ip)))

    copies = key_copies.by_source()

    assert copies == {
        leader.source: [f"the clone of group {installed.name} on relay {relay_server.name}"]
    }


def test_a_forwarded_group_has_no_copy(installed: ServerGroup) -> None:
    assert key_copies.by_source() == {}
