import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import Status
from apps.accounts.tests.factories import UserFactory
from apps.groups.models import GroupMember, ServerGroup
from apps.plans.services.limits import effective_limits
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.portal.services.servers import Health, my_servers
from apps.portal.tests.test_portal import subscriber
from apps.protocols.models import ProtocolInstance
from apps.servers.models import Server, ServerSample, ServerStatus
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def answered(server: Server, **sample: object) -> Server:
    server.record_sample(ServerSample.objects.create(server=server, at=timezone.now(), **sample))
    return server


def health(server: Server) -> Health:
    user = subscriber()
    (state,) = [s for s in my_servers(effective_limits(user)) if s.name == server.name]
    return state.health


def test_a_server_that_answers_is_working() -> None:
    assert health(answered(ServerFactory())) == Health.WORKING


def test_a_server_without_readings_has_no_data_yet() -> None:
    assert health(ServerFactory()) == Health.UNKNOWN


def test_a_silent_server_or_a_failed_check_is_a_problem() -> None:
    silent = answered(ServerFactory(), error="timeout")
    failed = answered(ServerFactory(status=ServerStatus.FAILED))

    assert health(silent) == Health.PROBLEMS
    assert health(failed) == Health.PROBLEMS


def test_a_stopped_container_is_a_problem() -> None:
    server = answered(ServerFactory(), containers=[{"name": "amnezia-awg2", "state": "exited"}])

    assert health(server) == Health.PROBLEMS


def test_a_stopped_probe_is_not_a_problem_for_clients() -> None:
    containers = [
        {"name": "amnezia-awg2", "state": "running"},
        {"name": "lamnezia-probe", "state": "exited"},
    ]
    server = answered(ServerFactory(probe_port=3478), containers=containers)

    assert health(server) == Health.WORKING


def test_maintenance_wins_over_everything() -> None:
    server = answered(ServerFactory(maintenance=True), error="timeout")

    assert health(server) == Health.MAINTENANCE


def test_only_servers_of_the_plan_are_listed() -> None:
    allowed, _other = ServerFactory(name="allowed"), ServerFactory(name="other")
    plan = PlanFactory()
    plan.servers.set([allowed])
    user = UserFactory()
    UserLimitsFactory(user=user, plan=plan, plan_assigned_at=timezone.now())

    assert [s.name for s in my_servers(effective_limits(user))] == ["allowed"]


def test_a_user_without_a_plan_sees_no_servers() -> None:
    ServerFactory()

    assert my_servers(effective_limits(UserFactory())) == []


def test_the_page_shows_status_but_not_the_address(client: Client) -> None:
    server = answered(
        ServerFactory(name="Frankfurt", location="Germany", host="198.51.100.7"),
        cpu_percent=42.5,
        uptime=12345,
    )
    client.force_login(subscriber())

    page = client.get("/portal/servers/").content.decode()

    assert "Frankfurt" in page
    assert "Germany" in page
    assert "working" in page
    assert server.host not in page
    assert "42.5" not in page
    assert "12345" not in page


def test_a_pending_user_sees_no_servers(client: Client) -> None:
    ServerFactory(name="Frankfurt")
    user = subscriber()
    user.status = Status.PENDING
    user.save()
    client.force_login(user)

    page = client.get("/portal/servers/").content.decode()

    assert "Frankfurt" not in page
    assert "No servers are available" in page


def grouped(group: ServerGroup, server: Server, *, leading: bool = False) -> Server:
    instance = ProtocolInstance.objects.create(
        server=server, container="amnezia-awg2", port=group.port
    )
    GroupMember.objects.create(group=group, instance=instance, priority=1, leading=leading)
    return server


def group_line(group: ServerGroup) -> tuple[str, Health]:
    (state,) = [s for s in my_servers(effective_limits(subscriber())) if s.name == group.name]
    return state.location, state.health


def test_a_group_is_one_line_without_its_members() -> None:
    group = ServerGroup.objects.create(name="Europe", port=51820)
    leader = grouped(group, answered(ServerFactory(name="de1")), leading=True)
    grouped(group, answered(ServerFactory(name="nl1")))
    ServerFactory(name="alone")

    states = my_servers(effective_limits(subscriber()))

    assert sorted(s.name for s in states) == ["Europe", "alone"]
    assert next(s for s in states if s.name == "Europe").pk == leader.pk


def test_a_group_works_while_one_member_answers() -> None:
    group = ServerGroup.objects.create(name="Europe", port=51820)
    grouped(group, answered(ServerFactory(), error="timeout"))
    grouped(group, answered(ServerFactory()))

    assert group_line(group) == ("", Health.WORKING)


def test_a_group_works_while_its_relay_answers() -> None:
    group = ServerGroup.objects.create(name="Europe", port=51820, relay=answered(ServerFactory()))
    grouped(group, answered(ServerFactory(), error="timeout"))

    assert group_line(group) == ("", Health.WORKING)


def test_a_group_whose_members_and_relay_are_silent_is_a_problem() -> None:
    relay = answered(ServerFactory(), error="timeout")
    group = ServerGroup.objects.create(name="Europe", port=51820, relay=relay)
    grouped(group, answered(ServerFactory(), error="timeout"))
    grouped(group, ServerFactory(maintenance=True))

    assert group_line(group) == ("", Health.PROBLEMS)


def test_the_page_shows_a_group_but_not_its_members(client: Client) -> None:
    group = ServerGroup.objects.create(name="Europe", port=51820)
    grouped(group, answered(ServerFactory(name="Frankfurt", host="198.51.100.7")))
    client.force_login(subscriber())

    page = client.get("/portal/servers/").content.decode()

    assert "Europe" in page
    assert "Frankfurt" not in page
    assert "198.51.100.7" not in page
