"""What needs a staff member's attention: VPS payments and failed operations."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.dashboard.services.attention import Attention
from apps.operations.tests.factories import FailedOperationFactory
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def paid_in(days: int) -> None:
    ServerFactory(name=f"vps{days}", paid_until=timezone.localdate() + timedelta(days=days))


def texts(attention: Attention) -> list[str]:
    return [notice.text for notice in attention.notices()]


def test_the_admin_is_reminded_of_vps_payments_soon_and_overdue() -> None:
    paid_in(3)
    paid_in(0)
    paid_in(-2)
    paid_in(30)

    notices = Attention(UserFactory(role=Role.ADMIN)).notices()

    assert {notice.text for notice in notices} == {
        "vps3: pay for the VPS within 3 days",
        "vps0: VPS payment is due today",
        "vps-2: VPS payment overdue by 2 days",
    }
    assert {notice.level for notice in notices if "overdue" in notice.text} == {"danger"}


def test_a_moderator_is_not_told_about_payments() -> None:
    paid_in(1)

    assert Attention(UserFactory(role=Role.MODERATOR)).payment_due() == []


def test_failed_operations_of_the_last_day_count() -> None:
    admin = UserFactory(role=Role.ADMIN)
    moderator = UserFactory(role=Role.MODERATOR)
    FailedOperationFactory(created_by=admin)
    FailedOperationFactory(created_by=moderator)

    assert Attention(admin).failed_operations == 2
    assert Attention(moderator).failed_operations == 1
    assert Attention(admin, now=timezone.now() + timedelta(days=2)).failed_operations == 0


def test_the_notice_names_how_many_operations_failed() -> None:
    admin = UserFactory(role=Role.ADMIN)
    FailedOperationFactory(created_by=admin)

    assert "1 operation failed in the last day" in texts(Attention(admin))


def test_without_trouble_there_are_no_notices() -> None:
    ServerFactory()

    assert Attention(UserFactory(role=Role.ADMIN)).notices() == []
