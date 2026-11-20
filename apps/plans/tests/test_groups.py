"""A plan gives server groups: the servers of a group whoever leads it, and its key by its name."""

import importlib

import pytest
from django.apps import apps

from apps.accounts.models import Role, User
from apps.accounts.services.zone import Zone
from apps.accounts.tests.factories import UserFactory
from apps.groups.models import GroupMember, MemberState, ServerGroup
from apps.groups.services.membership import delete_group
from apps.groups.tests import conftest as groups
from apps.groups.tests.conftest import JOINER_HOST, LEADER_FILES, instance_of
from apps.plans.forms import PlanForm
from apps.plans.models import Plan
from apps.plans.services.limits import effective_limits
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.portal.services.keys import MyKeys
from apps.protocols.models import ProtocolInstance
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

admin, group, leader = groups.admin, groups.group, groups.leader


@pytest.fixture
def member(group: ServerGroup) -> GroupMember:
    instance = instance_of(JOINER_HOST, LEADER_FILES)
    return GroupMember.objects.create(
        group=group, instance=instance, priority=1, state=MemberState.MEMBER
    )


def subscriber(plan: Plan) -> User:
    user: User = UserLimitsFactory(plan=plan).user
    return user


def test_a_group_allows_each_of_its_servers_and_no_other(
    group: ServerGroup, leader: ProtocolInstance, member: GroupMember
) -> None:
    plan = PlanFactory()
    plan.groups.add(group)

    limits = effective_limits(subscriber(plan))

    assert limits.allows_server(leader.server_id)
    assert limits.allows_server(member.instance.server_id)
    assert not limits.allows_server(ServerFactory().pk)


def test_the_key_of_the_group_goes_by_the_group_name(
    group: ServerGroup, leader: ProtocolInstance
) -> None:
    plan = PlanFactory()
    plan.groups.add(group)
    ProtocolInstance.objects.create(server=ServerFactory(), container="amnezia-awg2", port=1)

    offered = MyKeys(subscriber(plan)).instances()

    assert [(item.source, item.title) for item in offered] == [
        (leader.source, f"Europe · {leader.title}")
    ]


def test_a_new_leading_server_needs_no_plan_change(
    group: ServerGroup, leader: ProtocolInstance, member: GroupMember
) -> None:
    plan = PlanFactory()
    plan.groups.add(group)
    GroupMember.objects.filter(instance=leader).update(leading=False)
    GroupMember.objects.filter(pk=member.pk).update(leading=True)

    offered = MyKeys(subscriber(plan)).instances()

    assert [item.source for item in offered] == [member.instance.source]


def test_a_server_that_joins_a_group_is_given_by_the_group(group: ServerGroup) -> None:
    instance = instance_of(JOINER_HOST, LEADER_FILES)
    plan = PlanFactory()
    plan.servers.add(instance.server)

    GroupMember.objects.create(
        group=group, instance=instance, priority=1, state=MemberState.JOINING
    )

    assert list(plan.groups.all()) == [group] and not plan.servers.exists()


def test_the_leader_of_a_deleted_group_is_given_by_itself(
    group: ServerGroup, leader: ProtocolInstance, admin: User
) -> None:
    plan = PlanFactory()
    plan.groups.add(group)

    delete_group(group, actor=admin)

    assert list(plan.servers.all()) == [leader.server] and not plan.groups.exists()


def test_the_form_offers_groups_and_not_their_servers(
    group: ServerGroup, leader: ProtocolInstance
) -> None:
    standalone = ServerFactory()

    form = PlanForm(zone=Zone(UserFactory(role=Role.ADMIN)))

    assert list(form.fields["servers"].queryset) == [standalone]  # type: ignore[attr-defined]
    assert list(form.fields["groups"].queryset) == [group]  # type: ignore[attr-defined]


def test_the_migration_moves_servers_of_groups_to_the_groups(
    group: ServerGroup, leader: ProtocolInstance
) -> None:
    standalone = ServerFactory()
    plan = PlanFactory()
    Plan.servers.through.objects.bulk_create(
        [
            Plan.servers.through(plan=plan, server=leader.server),
            Plan.servers.through(plan=plan, server=standalone),
        ]
    )
    migration = importlib.import_module("apps.plans.migrations.0003_groups")

    migration.servers_to_groups(apps, None)

    assert list(plan.servers.all()) == [standalone] and list(plan.groups.all()) == [group]


def test_a_plan_with_nothing_chosen_still_allows_all(leader: ProtocolInstance) -> None:
    limits = effective_limits(subscriber(PlanFactory()))

    assert limits.server_ids is None
