from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from amnezia_core.xray import XRAY
from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import KeyStatus
from apps.keys.services.instances import Instance, instances
from apps.keys.tests.test_views import issued_key
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.portal.services.reachability import Probes, ProbeTarget, ServerProbe, probe_targets
from apps.servers.models import Server, ServerSample
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

KEYED_HOST = "203.0.113.50"
OTHER_HOST = "198.51.100.7"


def no_ssh() -> AbstractContextManager[RemoteHost]:
    raise AssertionError("the portal never connects to servers")


def instance(source: str, server: Server, host: str | None = None, **fields: Any) -> Instance:
    return Instance(source, server.name, host or server.host, no_ssh, server_id=server.pk, **fields)


def xray(source: str, server: Server, port: int, host: str | None = None) -> Instance:
    return instance(source, server, host, container=XRAY, port=port)


@pytest.fixture
def servers() -> Iterator[tuple[Server, Server]]:
    """Two servers with the probe; the user gets a key on the first one only."""
    keyed = ServerFactory(name="Amsterdam", host=KEYED_HOST, probe_port=3478)
    other = ServerFactory(name="Tokyo", host=OTHER_HOST, probe_port=3478)

    def provider() -> list[Instance]:
        return [instance("awg:keyed", keyed), instance("awg:other", other)]

    instances.register(provider)
    yield keyed, other
    instances.unregister(provider)


@pytest.fixture
def user() -> User:
    user = UserFactory()
    UserLimitsFactory(user=user, plan=PlanFactory(max_keys=3), plan_assigned_at=timezone.now())
    return user


@pytest.fixture
def user_client(client: Client, user: User) -> Client:
    client.force_login(user)
    return client


def test_pages_probe_only_servers_the_user_has_keys_on(
    user_client: Client, user: User, servers: tuple[Server, Server]
) -> None:
    key = issued_key(source="awg:keyed", user=user)

    for url in ("/portal/", "/portal/keys/new/", f"/portal/keys/{key.pk}/"):
        page = user_client.get(url).content.decode()
        assert f'data-probe="stun:{KEYED_HOST}:3478"' in page, url
        assert "js/reachability.js" in page, url
        assert OTHER_HOST not in page, url


def test_no_probe_without_keys_or_without_the_probe(
    user_client: Client, user: User, servers: tuple[Server, Server]
) -> None:
    issued_key(source="awg:keyed", user=user, status=KeyStatus.REVOKED)
    issued_key(source="awg:other", user=UserFactory())

    page = user_client.get("/portal/").content.decode()
    assert "data-probe" not in page
    assert "reachability.js" not in page

    keyed, _other = servers
    keyed.probe_port = None
    keyed.save()
    issued_key(source="awg:keyed", user=user)
    assert "data-probe" not in user_client.get("/portal/").content.decode()


def test_a_key_page_probes_only_the_server_of_that_key(
    user_client: Client, user: User, servers: tuple[Server, Server]
) -> None:
    issued_key(source="awg:other", user=user)
    key = issued_key(source="awg:keyed", user=user)

    page = user_client.get(f"/portal/keys/{key.pk}/").content.decode()

    assert KEYED_HOST in page
    assert OTHER_HOST not in page


def test_targets_are_one_per_address_with_ipv6_in_brackets() -> None:
    server = ServerFactory(name="Helsinki", host="2001:db8::1", probe_port=3479)
    plain = ServerFactory(name="Riga", host="vpn.example.org", probe_port=3478)
    keyed = [
        instance("awg:1", server),
        instance("awg:2", server),
        instance("awg:3", plain),
        instance("awg:4", plain, host="vpn2.example.org"),
    ]

    assert probe_targets(keyed) == [
        ProbeTarget("Helsinki", "stun:[2001:db8::1]:3479", server.pk),
        ProbeTarget("Riga", "stun:vpn.example.org:3478", plain.pk),
        ProbeTarget("Riga", "stun:vpn2.example.org:3478", plain.pk),
    ]


def test_xray_ports_are_probed_over_tcp_even_without_the_probe() -> None:
    server = ServerFactory(name="Oslo", host="203.0.113.9")
    v6 = ServerFactory(name="Bern", host="2001:db8::9")
    keyed = [
        xray("xray:1", server, 443),
        xray("xray:2", server, 443),
        xray("xray:3", server, 22),  # a port browsers never request
        xray("xray:4", v6, 443),  # CSP cannot name an IPv6 address
        instance("awg:1", server, port=55424),
    ]

    (target,) = probe_targets(keyed)

    assert target == ProbeTarget("Oslo", "https://203.0.113.9:443/", server.pk)
    assert target.origin == "https://203.0.113.9:443"
    assert ProbeTarget("Oslo", "stun:203.0.113.9:3478").origin is None


def test_a_server_with_a_stopped_probe_is_not_probed() -> None:
    server = ServerFactory(name="Oslo", host="203.0.113.9", probe_port=3478)
    containers = [{"name": "lamnezia-probe", "state": "exited"}]
    sample = ServerSample.objects.create(server=server, at=timezone.now(), containers=containers)
    server.record_sample(sample)

    assert probe_targets([instance("awg:1", server), xray("xray:1", server, 443)]) == [
        ProbeTarget("Oslo", "https://203.0.113.9:443/", server.pk)
    ]


def test_tcp_targets_are_allowed_by_the_page_policy_only(
    user_client: Client, user: User, servers: tuple[Server, Server]
) -> None:
    keyed, _other = servers
    issued_key(source="awg:keyed", user=user)
    plain = user_client.get("/portal/")
    assert "connect-src" not in plain["Content-Security-Policy"]

    def provider() -> list[Instance]:
        return [xray("xray:keyed", keyed, 8443)]

    instances.register(provider)
    try:
        issued_key(source="xray:keyed", user=user)
        response = user_client.get("/portal/")
    finally:
        instances.unregister(provider)

    assert f'https://{KEYED_HOST}:8443/"' in response.content.decode()
    policy = response["Content-Security-Policy"]
    assert policy.startswith("default-src 'self'")
    assert policy.endswith(f"; connect-src 'self' https://{KEYED_HOST}:8443")


def test_the_warning_offers_to_report_the_server(
    user_client: Client, user: User, servers: tuple[Server, Server]
) -> None:
    keyed, _other = servers
    issued_key(source="awg:keyed", user=user)

    page = user_client.get("/portal/").content.decode()

    warning = page[page.index("data-probe=") :]
    assert 'action="/support/server-down/"' in warning
    assert f'name="server" value="{keyed.pk}"' in warning


def test_probes_of_one_server_make_one_warning() -> None:
    near = ServerFactory(name="1Host", host="203.0.113.9", probe_port=3478)
    far = ServerFactory(name="IT-Garage", host="198.51.100.4", probe_port=3478)
    keyed = [
        instance("awg:1", near),
        instance("awg:2", near, host="vpn.example.org"),
        xray("xray:1", near, 443),
        instance("awg:3", far),
    ]

    assert Probes.of(keyed).servers == [
        ServerProbe(
            "1Host",
            near.pk,
            ("stun:203.0.113.9:3478", "stun:vpn.example.org:3478", "https://203.0.113.9:443/"),
        ),
        ServerProbe("IT-Garage", far.pk, ("stun:198.51.100.4:3478",)),
    ]


def test_the_page_warns_once_per_server(
    user_client: Client, user: User, servers: tuple[Server, Server]
) -> None:
    keyed, _other = servers

    def provider() -> list[Instance]:
        return [xray("xray:keyed", keyed, 8443)]

    instances.register(provider)
    try:
        issued_key(source="awg:keyed", user=user)
        issued_key(source="xray:keyed", user=user)
        page = user_client.get("/portal/").content.decode()
    finally:
        instances.unregister(provider)

    assert page.count("data-probe=") == 1
    assert f'data-probe="stun:{KEYED_HOST}:3478 https://{KEYED_HOST}:8443/"' in page
