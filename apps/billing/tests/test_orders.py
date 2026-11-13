from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from apps.accounts.models import Role, Status, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.billing.models import Order, OrderStatus
from apps.billing.services.orders import (
    Checkout,
    CheckoutRefusedError,
    PaymentMismatchError,
    UnknownOrderError,
    plans_for_sale,
    settle,
)
from apps.billing.services.providers import CheckoutUrls, PaymentProviderError
from apps.billing.tests.conftest import FakeProvider, notice
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.actions import KeyActions
from apps.keys.tests.conftest import SOURCE
from apps.keys.tests.fake_server import FakeAwgServer
from apps.plans.models import Plan, UserLimits
from apps.plans.services.limits import effective_limits
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.traffic.services.limits import LimitEnforcer

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
URLS = CheckoutUrls("https://panel.example.com/back", "https://panel.example.com/notify")


def monthly(**fields: Any) -> Plan:
    return PlanFactory(price=Decimal(100), currency="RUB", access_days=30, **fields)


def checkout(user: User, plan: Plan, provider: FakeProvider) -> Order:
    Checkout(user, "203.0.113.5").start(plan, provider, lambda order: URLS)
    return Order.objects.latest("pk")


def test_only_priced_plans_that_are_not_trials_are_sold() -> None:
    sold = monthly()
    monthly(is_trial=True)
    PlanFactory(price=None, currency="RUB")
    PlanFactory(price=Decimal(5), currency="")

    assert list(plans_for_sale()) == [sold]


def test_checkout_keeps_the_price_and_term_of_the_day(provider: FakeProvider) -> None:
    user = UserFactory()
    plan = monthly()

    address = Checkout(user).start(plan, provider, lambda order: URLS)

    order = Order.objects.get()
    assert address == f"https://pay.example.com/{order.number}"
    assert (order.user, order.plan, order.provider) == (user, plan, "fake")
    assert (order.amount, order.currency, order.days) == (Decimal(100), "RUB", 30)
    assert (order.status, order.external_id) == (OrderStatus.PENDING, f"ext-{order.pk}")
    assert provider.started == [URLS]
    assert AuditEvent.objects.filter(action="billing.checkout", actor=user).exists()


@pytest.mark.parametrize(
    ("user_fields", "plan_fields"),
    [
        ({"status": Status.PENDING}, {}),
        ({"status": Status.BLOCKED}, {}),
        ({}, {"is_trial": True}),
    ],
)
def test_checkout_is_refused(
    provider: FakeProvider, user_fields: dict[str, Any], plan_fields: dict[str, Any]
) -> None:
    with pytest.raises(CheckoutRefusedError):
        Checkout(UserFactory(**user_fields)).start(
            monthly(**plan_fields), provider, lambda order: URLS
        )

    assert not Order.objects.exists()


def test_order_the_service_refused_is_not_paid(provider: FakeProvider) -> None:
    provider.refuses = True

    with pytest.raises(PaymentProviderError):
        Checkout(UserFactory()).start(monthly(), provider, lambda order: URLS)

    assert Order.objects.get().status == OrderStatus.FAILED


def test_payment_renews_expired_access_and_turns_keys_back_on(
    awg_server: FakeAwgServer, provider: FakeProvider, django_capture_on_commit_callbacks: OnCommit
) -> None:
    plan = monthly()
    limits = UserLimitsFactory(plan=plan, plan_assigned_at=NOW - 40 * DAY)
    with django_capture_on_commit_callbacks(execute=True):
        KeyActions(UserFactory(role=Role.ADMIN)).issue(SOURCE, "iPhone", limits.user)
    with django_capture_on_commit_callbacks(execute=True):
        LimitEnforcer(NOW).enforce_all()
    key = AccessKey.objects.get()
    assert key.status == KeyStatus.DISABLED_LIMIT
    order = checkout(limits.user, plan, provider)

    with django_capture_on_commit_callbacks(execute=True):
        settle("fake", notice(order.number), NOW)

    order.refresh_from_db()
    assert (order.status, order.paid_at, order.external_id) == (OrderStatus.PAID, NOW, "pay-42")
    assert effective_limits(limits.user).access_until == NOW + 30 * DAY
    key.refresh_from_db()
    assert key.status == KeyStatus.ACTIVE
    assert AuditEvent.objects.filter(action="billing.paid").count() == 1


def test_a_payment_reported_twice_counts_once(provider: FakeProvider) -> None:
    plan = monthly()
    limits = UserLimitsFactory(plan=plan, plan_assigned_at=NOW)
    order = checkout(limits.user, plan, provider)

    settle("fake", notice(order.number), NOW)
    settle("fake", notice(order.number), NOW + DAY)

    assert effective_limits(limits.user).access_until == NOW + 60 * DAY
    assert AuditEvent.objects.filter(action="billing.paid").count() == 1


def test_another_plan_replaces_the_current_one(provider: FakeProvider) -> None:
    limits = UserLimitsFactory(plan_assigned_at=NOW - 3 * DAY)
    plan = monthly()
    order = checkout(limits.user, plan, provider)

    settle("fake", notice(order.number), NOW)

    limits.refresh_from_db()
    assert (limits.plan, limits.plan_assigned_at) == (plan, NOW)
    assert UserLimits.objects.get(user=limits.user).access_until == NOW + 30 * DAY


@pytest.mark.parametrize(("amount", "currency"), [("99.99", "RUB"), ("100", "USD")])
def test_a_payment_that_differs_from_the_order_is_refused(
    provider: FakeProvider, amount: str, currency: str
) -> None:
    order = checkout(UserFactory(), monthly(), provider)

    with pytest.raises(PaymentMismatchError):
        settle("fake", notice(order.number, amount=amount, currency=currency), NOW)

    order.refresh_from_db()
    assert order.status == OrderStatus.PENDING
    assert not UserLimits.objects.filter(user=order.user, plan__isnull=False).exists()


def test_currency_case_does_not_matter(provider: FakeProvider) -> None:
    order = checkout(UserFactory(), monthly(), provider)

    settle("fake", notice(order.number, amount="100.00", currency="rub"), NOW)

    order.refresh_from_db()
    assert order.status == OrderStatus.PAID


def test_a_failed_payment_is_recorded_and_money_paid_later_still_counts(
    provider: FakeProvider,
) -> None:
    user = UserFactory()
    order = checkout(user, monthly(), provider)

    settle("fake", notice(order.number, paid=False), NOW)
    order.refresh_from_db()
    assert order.status == OrderStatus.FAILED
    assert not effective_limits(user).has_plan

    settle("fake", notice(order.number), NOW)
    order.refresh_from_db()
    assert order.status == OrderStatus.PAID
    assert effective_limits(user).has_plan


def test_a_provider_cannot_settle_an_order_of_another_one(provider: FakeProvider) -> None:
    order = checkout(UserFactory(), monthly(), provider)

    with pytest.raises(UnknownOrderError):
        settle("other", notice(order.number), NOW)
    with pytest.raises(UnknownOrderError):
        settle("fake", notice("00000000-0000-0000-0000-000000000000"), NOW)
