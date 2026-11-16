from collections.abc import Callable

import pytest
from django.test import Client

from amnezia_core.awg import AwgServerConfig
from amnezia_core.awg_install import DEFAULT_TIMERS, SHORT_TIMERS
from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.groups.errors import GroupError, GroupErrorCode
from apps.groups.models import GroupMember, ServerGroup
from apps.groups.services.handover import start_handover
from apps.groups.services.membership import SETTINGS_KIND
from apps.groups.services.replication import CATCH_UP_KIND, catch_up_lagging
from apps.groups.services.settings import start_group_settings
from apps.groups.tests.conftest import (
    JOINER_HOST,
    LEADER_HOST,
    Fakes,
    Run,
    server_files,
    succeeded,
)
from apps.keys.services.profile import ServerProfile
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.awg_settings import AwgSettings
from apps.protocols.tests.awg_case import OnCommit, Start, issued_key

pytestmark = pytest.mark.django_db

NEW_SUBNET = "10.9.0.0/24"


class Change:
    """Runs a settings change of the group: the settings of the leader with some changed."""

    def __init__(self, group: ServerGroup, leader: ProtocolInstance, admin: User, start: Start):
        self._group = group
        self._leader = leader
        self._admin = admin
        self._start = start

    def __call__(
        self,
        *,
        params: dict[str, str] | None = None,
        dns: tuple[str, str] | None = None,
        subnet: str = "",
    ) -> Operation:
        self._leader.refresh_from_db()
        current = AwgSettings.of(self._leader)
        settings = AwgSettings(
            current.port,
            {**current.params, **(params or {})},
            dns or current.dns,
            current.logs,
            subnet or current.subnet,
        )
        return self._start(
            lambda: start_group_settings(self._group, settings, user=self._admin, reissue=False)
        )


@pytest.fixture
def joined(join: Run) -> GroupMember:
    succeeded(join())
    return GroupMember.objects.get(leading=False)


@pytest.fixture
def change(group: ServerGroup, leader: ProtocolInstance, admin: User, start: Start) -> Change:
    return Change(group, leader, admin, start)


@pytest.fixture
def catch_up(django_capture_on_commit_callbacks: OnCommit) -> Callable[[], list[Operation]]:
    def run() -> list[Operation]:
        with django_capture_on_commit_callbacks(execute=True):
            catch_up_lagging()
        return list(Operation.objects.filter(kind=CATCH_UP_KIND))

    return run


def config(servers: Fakes, host: str) -> AwgServerConfig:
    return AwgServerConfig.parse(server_files(servers[host]).config)


def lagging(member: GroupMember) -> bool:
    member.refresh_from_db()
    return member.lagging


def test_new_parameters_reach_every_member_and_keep_them_clones(
    servers: Fakes, joined: GroupMember, change: Change
) -> None:
    succeeded(change(params={"Jc": "7"}))

    assert server_files(servers[JOINER_HOST]) == server_files(servers[LEADER_HOST])
    assert config(servers, JOINER_HOST).awg_params["Jc"] == "7"
    assert not lagging(joined)
    assert {instance.params["Jc"] for instance in ProtocolInstance.objects.all()} == {"7"}


def test_new_subnet_moves_every_member_into_it(
    servers: Fakes, joined: GroupMember, change: Change
) -> None:
    succeeded(change(subnet=NEW_SUBNET))

    assert server_files(servers[JOINER_HOST]) == server_files(servers[LEADER_HOST])
    assert str(config(servers, JOINER_HOST).address.network) == NEW_SUBNET
    assert set(ProtocolInstance.objects.values_list("subnet", flat=True)) == {NEW_SUBNET}
    assert not lagging(joined)


def test_member_down_lags_and_catches_up_with_the_settings(
    servers: Fakes,
    down: set[str],
    joined: GroupMember,
    change: Change,
    catch_up: Callable[[], list[Operation]],
) -> None:
    down.add(JOINER_HOST)
    succeeded(change(params={"Jc": "7"}, subnet=NEW_SUBNET))

    assert lagging(joined)
    assert config(servers, JOINER_HOST).awg_params["Jc"] != "7"

    down.clear()
    (operation,) = catch_up()

    succeeded(operation)
    assert server_files(servers[JOINER_HOST]) == server_files(servers[LEADER_HOST])
    assert str(config(servers, JOINER_HOST).address.network) == NEW_SUBNET
    assert not lagging(joined)


def test_dns_alone_leaves_the_servers(servers: Fakes, joined: GroupMember, change: Change) -> None:
    before = server_files(servers[JOINER_HOST]), server_files(servers[LEADER_HOST])

    succeeded(change(dns=("9.9.9.9", "1.0.0.1")))

    assert (server_files(servers[JOINER_HOST]), server_files(servers[LEADER_HOST])) == before
    assert set(ProtocolInstance.objects.values_list("primary_dns", flat=True)) == {"9.9.9.9"}
    assert not lagging(joined)


def test_no_member_answers_nothing_changes(
    down: set[str], joined: GroupMember, change: Change
) -> None:
    down.update({LEADER_HOST, JOINER_HOST})

    operation = change(params={"Jc": "7"})

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == GroupErrorCode.NO_MEMBER_ANSWERS
    assert "7" not in {instance.params["Jc"] for instance in ProtocolInstance.objects.all()}
    assert not lagging(joined)


def test_member_that_refuses_the_settings_keeps_its_own_and_lags(
    servers: Fakes, joined: GroupMember, change: Change
) -> None:
    servers[JOINER_HOST].awg_refuses = "Jc = 7"

    succeeded(change(params={"Jc": "7"}))

    assert config(servers, LEADER_HOST).awg_params["Jc"] == "7"
    assert config(servers, JOINER_HOST).awg_params["Jc"] != "7"
    assert lagging(joined)
    joined.instance.refresh_from_db()
    assert joined.instance.params["Jc"] != "7"


def test_second_change_or_handover_waits_for_the_running_one(
    group: ServerGroup, leader: ProtocolInstance, joined: GroupMember, admin: User
) -> None:
    Operation.objects.create(
        kind=SETTINGS_KIND, status=OperationStatus.RUNNING, params={"group_id": group.pk}
    )

    with pytest.raises(GroupError) as change:
        start_group_settings(group, AwgSettings.of(leader), user=admin, reissue=False)
    with pytest.raises(GroupError) as handover:
        start_handover(joined, user=admin)

    assert change.value.code == handover.value.code == GroupErrorCode.SETTINGS_RUNNING


@pytest.fixture
def admin_client(client: Client, admin: User) -> Client:
    client.force_login(admin)
    return client


def test_member_settings_are_read_only_on_its_card(
    admin_client: Client, joined: GroupMember, group: ServerGroup
) -> None:
    url = f"/protocols/{joined.instance.pk}/settings/"

    page = admin_client.get(url)
    posted = admin_client.post(url, {"port": "1", "confirm": "on"})

    assert all(field.disabled for field in page.context["form"].fields.values())
    assert f"/servers/groups/{group.pk}/settings/" in page.content.decode()
    assert posted.headers["Location"] == f"/servers/groups/{group.pk}/settings/"
    assert not Operation.objects.exclude(kind__startswith="groups.").exists()


def test_group_page_starts_the_change(
    admin_client: Client, group: ServerGroup, leader: ProtocolInstance
) -> None:
    data = {
        "subnet": leader.subnet,
        "primary_dns": leader.primary_dns,
        "secondary_dns": leader.secondary_dns,
        **leader.params,
        "Jc": "7",
    }
    url = f"/servers/groups/{group.pk}/settings/"

    refused = admin_client.post(url, data)
    accepted = admin_client.post(url, {**data, "confirm": "on"})

    assert "confirm" in refused.context["form"].errors
    operation = Operation.objects.get(kind=SETTINGS_KIND)
    assert accepted.headers["Location"] == f"/operations/{operation.pk}/"
    assert operation.params["params"]["Jc"] == "7"
    assert operation.params["port"] == leader.port


def test_short_timers_reach_every_member_and_the_keys_of_the_group(
    servers: Fakes, leader: ProtocolInstance, joined: GroupMember, change: Change
) -> None:
    owner = UserFactory()
    key = issued_key(leader, owner)

    succeeded(change(params=SHORT_TIMERS))

    for host in (LEADER_HOST, JOINER_HOST):
        assert config(servers, host).awg_params.items() >= SHORT_TIMERS.items()
    key.refresh_from_db()
    assert ServerProfile.from_json(key.profile).params.items() >= SHORT_TIMERS.items()
    assert Notification.objects.get(event=Event.KEYS_CHANGED).user == owner
    assert not lagging(joined)


def settings_data(leader: ProtocolInstance, **changes: str) -> dict[str, str]:
    return {
        "subnet": leader.subnet,
        "primary_dns": leader.primary_dns,
        "secondary_dns": leader.secondary_dns,
        **leader.params,
        "confirm": "on",
        **changes,
    }


def posted_params(admin_client: Client, group: ServerGroup, data: dict[str, str]) -> dict[str, str]:
    admin_client.post(f"/servers/groups/{group.pk}/settings/", data)
    params: dict[str, str] = Operation.objects.get(kind=SETTINGS_KIND).params["params"]
    return params


def test_turning_short_timers_on_sets_them(
    admin_client: Client, group: ServerGroup, leader: ProtocolInstance
) -> None:
    params = posted_params(admin_client, group, settings_data(leader, short_timers="on"))

    assert params.items() >= SHORT_TIMERS.items()


def test_turning_short_timers_off_brings_the_timers_of_a_new_server_back(
    admin_client: Client, group: ServerGroup, leader: ProtocolInstance
) -> None:
    leader.params = {**leader.params, **SHORT_TIMERS}
    leader.save(update_fields=["params"])
    page = admin_client.get(f"/servers/groups/{group.pk}/settings/")

    params = posted_params(admin_client, group, settings_data(leader))

    assert page.context["form"]["short_timers"].initial is True
    assert params.items() >= DEFAULT_TIMERS.items()


def test_timers_typed_by_hand_stay_while_the_switch_is_untouched(
    admin_client: Client, group: ServerGroup, leader: ProtocolInstance
) -> None:
    data = settings_data(leader, KeepaliveTimeout="4-6", RekeyTimeout="2-4")

    params = posted_params(admin_client, group, data)

    assert (params["KeepaliveTimeout"], params["RekeyTimeout"]) == ("4-6", "2-4")
