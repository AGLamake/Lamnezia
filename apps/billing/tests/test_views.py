import json
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.billing.models import Order, OrderStatus
from apps.billing.tests.conftest import SECRET, FakeProvider
from apps.plans.models import Plan
from apps.plans.tests.factories import PlanFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def plan() -> Plan:
    return PlanFactory(name="Monthly", price=Decimal(100), currency="RUB", access_days=30)


def signed_in(client: Client) -> Client:
    client.force_login(UserFactory())
    return client


def notify(client: Client, order: Order, secret: str = SECRET, amount: str = "100") -> int:
    body = json.dumps({"order": str(order.number), "paid": True, "amount": amount})
    response = client.post(
        reverse("billing:notify", args=["fake"]),
        body,
        content_type="application/json",
        headers={"X-Secret": secret},
    )
    return response.status_code


def test_without_payment_plugins_there_is_no_payment(client: Client, plan: Plan) -> None:
    signed_in(client)

    assert client.get(reverse("billing:index")).status_code == 404
    assert client.post(reverse("billing:notify", args=["dummy"])).status_code == 404
    assert reverse("billing:index") not in client.get(reverse("portal:index")).content.decode()


def test_plans_on_sale_are_offered_with_each_provider(
    client: Client, plan: Plan, provider: FakeProvider
) -> None:
    signed_in(client)

    page = client.get(reverse("billing:index")).content.decode()

    assert "Monthly" in page
    assert 'value="fake"' in page
    assert reverse("billing:index") in client.get(reverse("portal:index")).content.decode()


def test_checkout_sends_the_user_to_the_payment_service(
    client: Client, plan: Plan, provider: FakeProvider
) -> None:
    signed_in(client)

    response = client.post(reverse("billing:checkout"), {"plan": plan.pk, "provider": "fake"})

    order = Order.objects.get()
    assert response.url == f"https://pay.example.com/{order.number}"  # type: ignore[attr-defined]
    assert provider.started[0].return_url.endswith(reverse("billing:order", args=[order.number]))
    assert provider.started[0].notify_url == "http://testserver/billing/notify/fake/"


def test_checkout_of_a_plan_not_on_sale_is_refused(client: Client, provider: FakeProvider) -> None:
    signed_in(client)
    trial = PlanFactory(price=Decimal(1), currency="RUB", is_trial=True)

    client.post(reverse("billing:checkout"), {"plan": trial.pk, "provider": "fake"})

    assert not Order.objects.exists()


def test_notification_needs_the_proof_of_the_provider(
    client: Client, plan: Plan, provider: FakeProvider
) -> None:
    signed_in(client).post(reverse("billing:checkout"), {"plan": plan.pk, "provider": "fake"})
    order = Order.objects.get()
    anonymous = Client()

    assert notify(anonymous, order, secret="guess") == 400
    assert notify(anonymous, order, amount="1") == 400
    assert notify(anonymous, order) == 200

    order.refresh_from_db()
    assert order.status == OrderStatus.PAID


def test_orders_of_other_users_are_hidden(
    client: Client, plan: Plan, provider: FakeProvider
) -> None:
    signed_in(Client()).post(reverse("billing:checkout"), {"plan": plan.pk, "provider": "fake"})
    order = Order.objects.get()

    signed_in(client)

    assert client.get(reverse("billing:order", args=[order.number])).status_code == 404


def test_a_paid_plan_stays_for_the_record(client: Client, plan: Plan) -> None:
    Order.objects.create(
        user=UserFactory(), plan=plan, provider="fake", amount=Decimal(100), currency="RUB"
    )
    client.force_login(UserFactory(role=Role.ADMIN))

    response = client.post(reverse("plans:delete", args=[plan.pk]), follow=True)

    assert Plan.objects.filter(pk=plan.pk).exists()
    assert "Clear its price to stop selling it" in response.content.decode()
