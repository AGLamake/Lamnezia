"""Key numbers at the top of the dashboard."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Role, Status
from apps.accounts.tests.factories import UserFactory
from apps.dashboard.services.overview import Overview, ServerCount
from apps.plans.models import UserLimits
from apps.plans.tests.factories import PlanFactory
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def test_servers_count_those_that_answer() -> None:
    admin = UserFactory(role=Role.ADMIN)
    servers: list[Server] = [ServerFactory(), ServerFactory()]

    assert Overview(admin, servers).servers() == ServerCount(online=0, total=2)


def test_users_are_active_clients_only() -> None:
    admin = UserFactory(role=Role.ADMIN)
    UserFactory(role=Role.USER, status=Status.ACTIVE)
    UserFactory(role=Role.USER, status=Status.BLOCKED)
    UserFactory(role=Role.MODERATOR, status=Status.ACTIVE)

    assert Overview(admin, []).users() == 1


def test_without_traffic_today_and_the_week_are_zero() -> None:
    overview = Overview(UserFactory(role=Role.ADMIN), [])

    assert overview.today() == 0
    assert overview.week_total() == 0


def test_a_moderator_counts_own_clients_whose_access_ends_soon() -> None:
    moderator = UserFactory(role=Role.MODERATOR)
    now = timezone.now()
    plan = PlanFactory()
    for owner, days in ((moderator, 3), (moderator, 30), (moderator, -1), (None, 3)):
        client = UserFactory(role=Role.USER, status=Status.ACTIVE, manager=owner)
        UserLimits.objects.create(user=client, plan=plan, access_until=now + timedelta(days=days))

    assert Overview(moderator, [], now).access_ending() == 1
