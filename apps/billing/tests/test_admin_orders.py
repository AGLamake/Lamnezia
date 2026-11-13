from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, Status, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.billing.models import Order, OrderStatus
from apps.billing.services.orders import RefundRefusedError, expire, refund, settle
from apps.billing.services.providers import PaymentProviderError, payment_providers
from apps.billing.tests.conftest import FakeProvider, notice
from apps.billing.tests.test_orders import NOW, checkout, monthly
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.notifications.tests.factories import ChannelFactory
from apps.plans.services.limits import effective_limits
from apps.plans.tests.factories import UserLimitsFactory

pytestmark = pytest.mark.django_db


class RefundingProvider(FakeProvider):
    refunds = True

    def __init__(self) -> None:
        super().__init__()
        self.refunded: list[Order] = []

    def refund(self, order: Order) -> None:
        if self.refuses:
            raise PaymentProviderError
        self.refunded.append(order)


@pytest.fixture
def refunding() -> Iterator[RefundingProvider]:
    """The fake provider, able to return money, in place of `provider`."""
    fake = RefundingProvider()
    payment_providers.register(fake)
    yield fake
    payment_providers.unregister(fake.key)


def admin(client: Client) -> User:
    user = UserFactory(role=Role.ADMIN)
    client.force_login(user)
    return user


def paid(provider: FakeProvider, user: User | None = None) -> Order:
    plan = monthly()
    buyer = user or UserLimitsFactory(plan=plan, plan_assigned_at=NOW).user
    order = checkout(buyer, plan, provider)
    settle("fake", notice(order.number), NOW)
    order.refresh_from_db()
    return order


class TestHistory:
    def test_an_order_outlives_its_user(self, provider: FakeProvider) -> None:
        user = UserFactory()
        order = paid(provider, user)

        user.delete()

        order.refresh_from_db()
        assert (order.user, order.buyer, order.status) == (None, user.username, OrderStatus.PAID)

    def test_a_payment_for_a_deleted_user_is_recorded(self, provider: FakeProvider) -> None:
        user = UserFactory()
        order = checkout(user, monthly(), provider)
        user.delete()

        settle("fake", notice(order.number), NOW)

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID


class TestStaffNotice:
    def test_a_payment_is_reported_to_the_staff_channels(self, provider: FakeProvider) -> None:
        ChannelFactory(events=[Event.PAYMENT_RECEIVED])

        order = paid(provider)
        settle("fake", notice(order.number), NOW)

        [sent] = Notification.objects.filter(event=Event.PAYMENT_RECEIVED)
        assert sent.data == {
            "user": order.buyer,
            "plan": order.plan.name,
            "amount": "100.00",
            "currency": "RUB",
            "provider": "fake",
        }


class TestBlocked:
    def test_a_blocked_buyer_gets_the_term_but_no_keys_turned_on(
        self, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = monthly()
        limits = UserLimitsFactory(plan=plan, plan_assigned_at=NOW - 40 * timedelta(days=1))
        order = checkout(limits.user, plan, provider)
        User.objects.filter(pk=limits.user.pk).update(status=Status.BLOCKED)
        switched: list[Any] = []
        monkeypatch.setattr(
            "apps.traffic.services.limits.LimitEnforcer.switch",
            lambda self, *args: switched.append(args),
        )

        settle("fake", notice(order.number), NOW)

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert effective_limits(limits.user).access_until == NOW + 30 * timedelta(days=1)
        assert switched == []


class TestExpiry:
    def test_orders_left_waiting_too_long_expire(self, provider: FakeProvider) -> None:
        old = checkout(UserFactory(), monthly(), provider)
        fresh = checkout(UserFactory(), monthly(name="Other"), provider)
        done = paid(provider)
        now = timezone.now()
        Order.objects.filter(pk__in=[old.pk, done.pk]).update(created_at=now - timedelta(days=2))

        assert expire(now) == 1

        statuses = dict(Order.objects.values_list("pk", "status"))
        assert statuses == {
            old.pk: OrderStatus.EXPIRED,
            fresh.pk: OrderStatus.PENDING,
            done.pk: OrderStatus.PAID,
        }

    def test_money_for_an_expired_order_still_counts(self, provider: FakeProvider) -> None:
        order = checkout(UserFactory(), monthly(), provider)
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.EXPIRED)

        settle("fake", notice(order.number), NOW)

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID


class TestRefund:
    def test_a_refund_returns_the_money_and_keeps_the_access(
        self, refunding: RefundingProvider
    ) -> None:
        user = UserFactory()
        order = paid(refunding, user)
        until = effective_limits(user).access_until
        actor = UserFactory(role=Role.ADMIN)

        refund(order, actor)

        order.refresh_from_db()
        assert order.status == OrderStatus.REFUNDED
        assert refunding.refunded == [order]
        assert effective_limits(user).access_until == until
        assert AuditEvent.objects.filter(action="billing.refunded", actor=actor).exists()

    def test_a_repeated_report_does_not_pay_a_refunded_order_again(
        self, refunding: RefundingProvider
    ) -> None:
        order = paid(refunding)
        refund(order, UserFactory(role=Role.ADMIN))

        settle("fake", notice(order.number), NOW)

        order.refresh_from_db()
        assert order.status == OrderStatus.REFUNDED
        assert AuditEvent.objects.filter(action="billing.paid").count() == 1

    def test_a_provider_without_refunds_refuses(self, provider: FakeProvider) -> None:
        order = paid(provider)

        with pytest.raises(RefundRefusedError):
            refund(order, UserFactory(role=Role.ADMIN))

    def test_an_unpaid_order_is_not_refunded(self, refunding: RefundingProvider) -> None:
        order = checkout(UserFactory(), monthly(), refunding)

        with pytest.raises(RefundRefusedError):
            refund(order, UserFactory(role=Role.ADMIN))

    def test_a_refused_refund_leaves_the_order_paid(self, refunding: RefundingProvider) -> None:
        order = paid(refunding)
        refunding.refuses = True

        with pytest.raises(PaymentProviderError):
            refund(order, UserFactory(role=Role.ADMIN))

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID


class TestOrdersPage:
    def test_the_administrator_sees_and_filters_orders(
        self, client: Client, provider: FakeProvider
    ) -> None:
        done = paid(provider)
        waiting = checkout(UserFactory(username="zoe"), monthly(name="Other"), provider)
        admin(client)
        url = reverse("billing:orders")

        everything = client.get(url)
        by_status = client.get(url, {"status": OrderStatus.PAID})
        by_user = client.get(url, {"user": "zo"})

        assert set(everything.context["orders"]) == {done, waiting}
        assert list(by_status.context["orders"]) == [done]
        assert list(by_user.context["orders"]) == [waiting]
        assert reverse("billing:orders") in everything.content.decode()

    def test_a_deleted_buyer_is_found_by_their_old_name(
        self, client: Client, provider: FakeProvider
    ) -> None:
        user = UserFactory(username="gone")
        order = paid(provider, user)
        user.delete()
        admin(client)

        page = client.get(reverse("billing:orders"), {"user": "gone"})

        assert list(page.context["orders"]) == [order]
        assert "gone" in page.content.decode()

    @pytest.mark.parametrize("role", [Role.MODERATOR, Role.USER])
    def test_orders_are_for_administrators_only(
        self, client: Client, provider: FakeProvider, role: Role
    ) -> None:
        order = paid(provider)
        client.force_login(UserFactory(role=role))

        assert client.get(reverse("billing:orders")).status_code == 403
        assert client.post(reverse("billing:refund", args=[order.number])).status_code == 403

    def test_refund_from_the_list(self, client: Client, refunding: RefundingProvider) -> None:
        order = paid(refunding)
        admin(client)

        assert "Refund" in client.get(reverse("billing:orders")).content.decode()
        response = client.post(reverse("billing:refund", args=[order.number]))

        assert response.status_code == 302
        order.refresh_from_db()
        assert order.status == OrderStatus.REFUNDED

    def test_no_refund_button_without_a_refunding_provider(
        self, client: Client, provider: FakeProvider
    ) -> None:
        paid(provider)
        admin(client)

        assert "Refund" not in client.get(reverse("billing:orders")).content.decode()


def test_the_nav_shows_orders_only_with_a_plugin(client: Client) -> None:
    admin(client)
    assert reverse("billing:orders") not in client.get(reverse("dashboard:index")).content.decode()
