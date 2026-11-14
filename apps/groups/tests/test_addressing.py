"""Keys of a group behind its relay: their address, the DNS check and whose traffic is whose."""

import json

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.groups.models import GroupMember, ServerGroup
from apps.groups.services.addressing import (
    DomainCheck,
    GroupKeyAddress,
    client_host,
    client_hosts,
    key_sources,
)
from apps.groups.services.membership import create_group
from apps.groups.tests.conftest import JOINER_HOST, LEADER_HOST, PORT, Run, succeeded
from apps.keys.models import AccessKey
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.protocols.models import ProtocolInstance
from apps.protocols.providers import key_instances, traffic_targets
from apps.protocols.tests.awg_case import issued_key
from apps.relays.models import Relay
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.support.services.context import keys_seen
from apps.traffic.models import TrafficCounter

pytestmark = pytest.mark.django_db

RELAY_HOST = "203.0.113.30"
DOMAIN = "vpn.example.com"


@pytest.fixture
def relay_server() -> Server:
    return ServerFactory(host=RELAY_HOST)


def install(server: Server) -> None:
    Relay.objects.update_or_create(server=server, defaults={"installed_at": timezone.now()})


def behind(group: ServerGroup, relay: Server, domain: str = "") -> ServerGroup:
    group.relay, group.domain = relay, domain
    group.save(update_fields=["relay", "domain"])
    return group


def endpoint(key: AccessKey) -> tuple[str, int]:
    key.refresh_from_db()
    profile = json.loads(key.profile)
    return profile["host"], profile["port"]


def host_of_keys(source: str) -> str:
    return next(instance.host for instance in key_instances() if instance.source == source)


def test_keys_go_to_the_leading_server_until_the_relay_is_installed(
    group: ServerGroup, leader: ProtocolInstance, relay_server: Server
) -> None:
    behind(group, relay_server, DOMAIN)
    assert client_host(group) is None

    Relay.objects.create(server=relay_server)
    assert client_host(group) is None
    assert host_of_keys(leader.source) == LEADER_HOST


def test_keys_of_a_relayed_group_carry_its_domain_or_the_relay_address(
    group: ServerGroup, leader: ProtocolInstance, relay_server: Server
) -> None:
    install(relay_server)
    behind(group, relay_server)
    assert client_hosts() == {leader.source: RELAY_HOST}

    behind(group, relay_server, DOMAIN)
    assert client_host(group) == DOMAIN
    assert host_of_keys(leader.source) == DOMAIN


def test_earlier_keys_move_to_the_relay_and_owners_are_told_once(
    group: ServerGroup, leader: ProtocolInstance, relay_server: Server
) -> None:
    owner = UserFactory()
    first, second = issued_key(leader, owner), issued_key(leader, owner)
    install(relay_server)
    behind(group, relay_server, DOMAIN)

    assert GroupKeyAddress(group).apply() == 2

    assert endpoint(first) == endpoint(second) == (DOMAIN, PORT)
    notification = Notification.objects.get(event=Event.KEYS_UPDATED)
    assert (notification.user, notification.data["server"]) == (owner, leader.server.name)
    assert GroupKeyAddress(group).outdated == []


def test_new_group_behind_a_running_relay_moves_the_keys_of_its_leader(
    leader: ProtocolInstance, relay_server: Server, admin: User
) -> None:
    key = issued_key(leader, UserFactory())
    install(relay_server)

    create_group(ServerGroup(name="Europe", relay=relay_server), leader, actor=admin)

    assert endpoint(key) == (RELAY_HOST, PORT)


def test_traffic_of_a_member_counts_for_the_keys_of_the_leader(
    leader: ProtocolInstance, joiner: ProtocolInstance, join: Run
) -> None:
    assert key_sources() == {}  # a joining server does not run the group identity yet

    succeeded(join())

    assert key_sources() == {joiner.source: leader.source}
    by_name = {target.name: target for target in traffic_targets()}
    assert by_name[joiner.server.name].key_sources == {joiner.source: leader.source}
    assert by_name[leader.server.name].key_sources == {}


def test_a_group_key_is_seen_on_any_member(
    group: ServerGroup, leader: ProtocolInstance, joiner: ProtocolInstance
) -> None:
    GroupMember.objects.create(group=group, instance=joiner, priority=1)
    owner = UserFactory()
    key = issued_key(leader, owner)
    seen = timezone.now()
    TrafficCounter.objects.create(
        source=joiner.source,
        key_source=leader.source,
        public_key=key.public_key,
        latest_handshake=seen,
        collected_at=seen,
    )

    assert [entry.seen for entry in keys_seen(owner)] == [seen]


class TestDomainCheck:
    @staticmethod
    def warnings(group: ServerGroup, ipv4: dict[str, list[str]], ipv6: list[str]) -> list[str]:
        def resolve(name: str) -> list[str]:
            if name not in ipv4:
                raise OSError(name)
            return ipv4[name]

        return DomainCheck(group, resolve, lambda _name: ipv6).warnings()

    def test_domain_with_the_relay_address_only_is_fine(
        self, group: ServerGroup, relay_server: Server
    ) -> None:
        behind(group, relay_server, DOMAIN)

        assert self.warnings(group, {DOMAIN: [RELAY_HOST], RELAY_HOST: [RELAY_HOST]}, []) == []

    def test_domain_that_does_not_resolve(self, group: ServerGroup, relay_server: Server) -> None:
        behind(group, relay_server, DOMAIN)

        [warning] = self.warnings(group, {RELAY_HOST: [RELAY_HOST]}, [])
        assert "does not resolve" in warning

    def test_domain_pointing_elsewhere_and_with_an_aaaa_record(
        self, group: ServerGroup, relay_server: Server
    ) -> None:
        behind(group, relay_server, DOMAIN)

        other, aaaa = self.warnings(
            group, {DOMAIN: [JOINER_HOST], RELAY_HOST: [RELAY_HOST]}, ["2001:db8::1"]
        )
        assert JOINER_HOST in other
        assert "AAAA" in aaaa

    def test_no_domain_nothing_to_check(self, group: ServerGroup, relay_server: Server) -> None:
        behind(group, relay_server)

        assert self.warnings(group, {}, [LEADER_HOST]) == []
