from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.billing.models import Order, OrderStatus
from apps.plans.models import Plan
from apps.plans.services.limits import effective_limits
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from examples.billing_dummy.provider import sign_notice

pytestmark = pytest.mark.django_db


@pytest.fixture
def plan() -> Plan:
    return PlanFactory(price=Decimal(150), currency="RUB", access_days=30)


@pytest.fixture
def debug(settings: Any) -> None:
    settings.DEBUG = True


def pay_page(client: Client, plan: Plan) -> str:
    response = client.post(reverse("billing:checkout"), {"plan": plan.pk, "provider": "dummy"})
    return str(response.url)  # type: ignore[attr-defined]


def test_test_payment_extends_access(client: Client, plan: Plan, debug: None) -> None:
    limits = UserLimitsFactory(plan=plan, plan_assigned_at=timezone.now() - timedelta(days=10))
    client.force_login(limits.user)
    end = effective_limits(limits.user).access_until
    assert end is not None

    page = client.get(pay_page(client, plan))
    order = Order.objects.get()
    paid = page.context["paid_notice"]
    response = client.post(reverse("billing:notify", args=["dummy"]), {"notice": paid})

    assert response.url == reverse("billing:order", args=[order.number])  # type: ignore[attr-defined]
    order.refresh_from_db()
    assert order.status == OrderStatus.PAID
    assert effective_limits(limits.user).access_until == end + timedelta(days=30)


def test_refused_test_payment_changes_nothing(client: Client, plan: Plan, debug: None) -> None:
    user = UserFactory()
    client.force_login(user)
    page = client.get(pay_page(client, plan))

    client.post(
        reverse("billing:notify", args=["dummy"]), {"notice": page.context["failed_notice"]}
    )

    assert Order.objects.get().status == OrderStatus.FAILED
    assert not effective_limits(user).has_plan


def test_a_forged_notice_is_refused(client: Client, plan: Plan, debug: None) -> None:
    client.force_login(UserFactory())
    client.get(pay_page(client, plan))
    order = Order.objects.get()
    signed = sign_notice(order, paid=True)
    forged = signed[:-1] + ("A" if signed[-1] != "A" else "B")

    for notice in (forged, "not-signed", ""):
        response = client.post(reverse("billing:notify", args=["dummy"]), {"notice": notice})
        assert response.status_code == 400
    order.refresh_from_db()
    assert order.status == OrderStatus.PENDING


def test_pay_page_shows_only_own_pending_orders(client: Client, plan: Plan, debug: None) -> None:
    owner = Client()
    owner.force_login(UserFactory())
    address = pay_page(owner, plan)
    client.force_login(UserFactory())

    assert owner.get(address).status_code == 200
    assert client.get(address).status_code == 404


def test_without_debug_the_test_payment_is_off(client: Client, plan: Plan) -> None:
    client.force_login(UserFactory())

    assert client.get(reverse("billing:index")).status_code == 404
    client.post(reverse("billing:checkout"), {"plan": plan.pk, "provider": "dummy"})
    assert not Order.objects.exists()
