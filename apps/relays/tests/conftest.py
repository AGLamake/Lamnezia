"""Fixtures of the relay tests: the group fixtures plus a fake relay server.

`servers` of the groups connects the members; `relay_server` also connects the deployment.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from apps.groups.models import GroupMember, ServerGroup
from apps.groups.tests import conftest as groups
from apps.groups.tests.conftest import Fakes, Run, succeeded
from apps.relays.tests.fake_relay import FakeRelayServer
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

RELAY_HOST = "203.0.113.30"

# The fixtures of the group tests, by name for pytest.
admin, down, group, join, joiner = (
    groups.admin,
    groups.down,
    groups.group,
    groups.join,
    groups.joiner,
)
leader, leader_awg, joiner_awg = groups.leader, groups.leader_awg, groups.joiner_awg
servers, start = groups.servers, groups.start


@pytest.fixture
def relay_fake() -> FakeRelayServer:
    return FakeRelayServer()


@pytest.fixture
def relay_server(
    relay_fake: FakeRelayServer, servers: Fakes, monkeypatch: pytest.MonkeyPatch
) -> Server:
    """The relay machine, connected by the deployment; members connect as in the group tests."""

    @contextmanager
    def connect(server: Server) -> Iterator[RemoteHost]:
        if server.host == RELAY_HOST:
            yield relay_fake.host()
        else:
            yield servers[server.host].host()

    monkeypatch.setattr("apps.relays.services.deployment.connect", connect)
    return ServerFactory(host=RELAY_HOST)


@pytest.fixture
def joined(join: Run) -> GroupMember:
    succeeded(join())
    return GroupMember.objects.get(leading=False)


@pytest.fixture
def relayed(group: ServerGroup, joined: GroupMember, relay_server: Server) -> ServerGroup:
    """The group of two members behind the relay server."""
    group.relay = relay_server
    group.save(update_fields=["relay"])
    return group
