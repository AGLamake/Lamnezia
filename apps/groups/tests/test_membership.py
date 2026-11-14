import pytest

from apps.accounts.models import User
from apps.groups.errors import GroupError, GroupErrorCode
from apps.groups.models import GroupMember, MemberState, ServerGroup
from apps.groups.services.membership import (
    check_standalone,
    create_group,
    delete_group,
    standalone_instances,
    start_join,
    start_leave,
)
from apps.groups.tests.conftest import JOINER_HOST, LEADER_FILES, PORT, instance_of
from apps.keys.services.instances import instances
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import APP_CONFIG
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def test_new_group_takes_the_leader_as_it_is(group: ServerGroup, leader: ProtocolInstance) -> None:
    member = group.members.get()

    assert group.port == leader.port == PORT
    assert member.instance == leader
    assert member.leading
    assert member.state == MemberState.MEMBER
    assert group.leader == member


def test_server_of_a_group_is_not_standalone(
    group: ServerGroup, leader: ProtocolInstance, joiner: ProtocolInstance, admin: User
) -> None:
    assert standalone_instances() == [joiner]
    with pytest.raises(GroupError) as error:
        create_group(ServerGroup(name="Asia"), leader, actor=admin)
    assert error.value.code == GroupErrorCode.IN_GROUP
    with pytest.raises(GroupError) as error:
        start_join(group, leader, user=admin)
    assert error.value.code == GroupErrorCode.IN_GROUP


def test_awg2_server_cannot_join(group: ServerGroup, admin: User) -> None:
    old = ProtocolInstance.objects.create(
        server=ServerFactory(),
        container="amnezia-awg2",
        port=PORT,
        subnet="10.8.1.0/24",
        params=APP_CONFIG.awg_params,
    )

    with pytest.raises(GroupError) as error:
        check_standalone(old)

    assert error.value.code == GroupErrorCode.NOT_AWG3


def test_server_on_another_port_cannot_join(
    group: ServerGroup, joiner: ProtocolInstance, admin: User
) -> None:
    joiner.port = PORT + 1
    joiner.save()

    with pytest.raises(GroupError) as error:
        start_join(group, joiner, user=admin)

    assert error.value.code == GroupErrorCode.OTHER_PORT
    assert not GroupMember.objects.filter(instance=joiner).exists()


def test_server_in_another_subnet_cannot_join(
    group: ServerGroup, joiner: ProtocolInstance, admin: User
) -> None:
    joiner.subnet = "10.9.1.0/24"
    joiner.save()

    with pytest.raises(GroupError) as error:
        start_join(group, joiner, user=admin)

    assert error.value.code == GroupErrorCode.OTHER_SUBNET


def test_leading_server_cannot_leave(group: ServerGroup, admin: User) -> None:
    assert group.leader is not None

    with pytest.raises(GroupError) as error:
        start_leave(group.leader, user=admin)

    assert error.value.code == GroupErrorCode.LEADER_LEAVES


def test_group_with_members_is_not_deleted(group: ServerGroup, admin: User) -> None:
    GroupMember.objects.create(
        group=group, instance=instance_of(JOINER_HOST, LEADER_FILES), priority=1
    )

    with pytest.raises(GroupError) as error:
        delete_group(group, actor=admin)

    assert error.value.code == GroupErrorCode.MEMBERS_LEFT


def test_deleted_group_leaves_its_leader_standalone(
    group: ServerGroup, leader: ProtocolInstance, admin: User
) -> None:
    delete_group(group, actor=admin)

    assert not ServerGroup.objects.exists()
    assert standalone_instances() == [leader]


def test_keys_are_issued_on_the_leading_server_only(
    group: ServerGroup, leader: ProtocolInstance
) -> None:
    clone = instance_of(JOINER_HOST, LEADER_FILES)
    GroupMember.objects.create(group=group, instance=clone, priority=1)
    standalone = instance_of("203.0.113.30", LEADER_FILES)

    sources = set(instances.titles())

    assert leader.source in sources
    assert standalone.source in sources
    assert clone.source not in sources
