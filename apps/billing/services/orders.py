"""Orders: a user buys a plan through a provider, and a confirmed payment renews the access."""

from collections.abc import Callable
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.billing.models import Order, OrderStatus
from apps.billing.services.providers import (
    CheckoutUrls,
    PaymentNotice,
    PaymentProvider,
    PaymentProviderError,
    payment_providers,
)
from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.plans.models import Plan
from apps.plans.services.changes import apply_speed_if_changed
from apps.plans.services.limits import effective_limits
from apps.plans.services.renewal import renew
from apps.traffic.services.limits import LimitEnforcer


class CheckoutRefusedError(Exception):
    """The user may not buy this plan now."""


class UnknownOrderError(Exception):
    """A notification names an order this provider never started."""


class PaymentMismatchError(Exception):
    """The amount or currency paid differs from the order."""


class RefundRefusedError(Exception):
    """The order is not paid, or its provider is gone or cannot return money."""


def plans_for_sale() -> QuerySet[Plan]:
    """Plans with a price and a currency; trial plans are given by an admin, not sold."""
    return Plan.objects.filter(price__gt=0, is_trial=False).exclude(currency="")


class Checkout:
    """Starts the payment of a plan: an order at today's price, then the provider's page."""

    def __init__(self, user: User, ip: str | None = None) -> None:
        self._user = user
        self._ip = ip

    def start(
        self,
        plan: Plan,
        provider: PaymentProvider,
        urls_for: Callable[[Order], CheckoutUrls],
    ) -> str:
        """The address to send the user to; a refused order is kept as not paid."""
        if not self._user.can_have_keys or not plans_for_sale().filter(pk=plan.pk).exists():
            raise CheckoutRefusedError
        assert plan.price is not None
        order = Order.objects.create(
            user=self._user,
            username=self._user.username,
            plan=plan,
            provider=provider.key,
            amount=plan.price,
            currency=plan.currency,
            days=plan.access_days,
        )
        audit.log(
            "billing.checkout",
            actor=self._user,
            target=self._user,
            data={"order": str(order.number), "plan_id": plan.pk, "provider": provider.key},
            ip=self._ip,
        )
        try:
            address = provider.start(order, urls_for(order))
        except PaymentProviderError:
            order.status = OrderStatus.FAILED
            order.save(update_fields=["status"])
            raise
        order.save(update_fields=["external_id"])
        return address


def settle(provider: str, notice: PaymentNotice, now: datetime) -> Order:
    """Apply a checked notification: a payment renews the access once, a failure is recorded.

    The order row is locked, so notifications that arrive together apply the payment once. Money
    that arrives is never turned away: an expired order is paid all the same, and a deleted or
    blocked buyer's payment is recorded for the staff to decide on (`_renew`).
    """
    with transaction.atomic():
        order = (
            Order.objects.select_for_update(of=("self",))
            .select_related("user", "plan")
            .filter(number=notice.order_number, provider=provider)
            .first()
        )
        if order is None:
            raise UnknownOrderError
        if order.status in (OrderStatus.PAID, OrderStatus.REFUNDED):
            return order
        if not notice.paid:
            order.status = OrderStatus.FAILED
            order.save(update_fields=["status"])
            return order
        if notice.amount != order.amount or notice.currency.upper() != order.currency.upper():
            raise PaymentMismatchError
        _mark_paid(order, notice, now)
        if order.user is not None:
            _renew(order.user, order, now)
    return order


def expire(now: datetime) -> int:
    """Orders left waiting for payment longer than `ORDER_EXPIRES_AFTER`; returns how many."""
    stale = Order.objects.filter(
        status=OrderStatus.PENDING, created_at__lt=now - settings.ORDER_EXPIRES_AFTER
    )
    return stale.update(status=OrderStatus.EXPIRED)


def refund(order: Order, actor: User, ip: str | None = None) -> None:
    """Return the money of a paid order through its provider; the access is left as it is."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        provider = payment_providers.get(order.provider)
        if order.status != OrderStatus.PAID or provider is None or not provider.refunds:
            raise RefundRefusedError
        provider.refund(order)
        order.status = OrderStatus.REFUNDED
        order.save(update_fields=["status"])
        audit.log(
            "billing.refunded",
            actor=actor,
            target=order.user,
            data={"order": str(order.number), "amount": str(order.amount)},
            ip=ip,
        )


def _mark_paid(order: Order, notice: PaymentNotice, now: datetime) -> None:
    """The payment is recorded, written to the audit log and reported to the staff."""
    order.status = OrderStatus.PAID
    order.paid_at = now
    order.external_id = notice.external_id or order.external_id
    order.save(update_fields=["status", "paid_at", "external_id"])
    audit.log(
        "billing.paid",
        actor=None,
        target=order.user,
        data={
            "order": str(order.number),
            "plan_id": order.plan_id,
            "amount": str(order.amount),
            "currency": order.currency,
            "provider": order.provider,
        },
    )
    emit(
        Event.PAYMENT_RECEIVED,
        {
            "user": order.buyer or "—",
            "plan": order.plan.name,
            "amount": str(order.amount),
            "currency": order.currency,
            "provider": order.provider,
        },
        key=f"payment_received:{order.number}",
    )


def _renew(user: User, order: Order, now: datetime) -> None:
    """New limits at once, and keys turned on or off now instead of at the next enforcer run.

    A blocked or not yet approved buyer gets the term they paid for, but no keys are turned on
    until an administrator lets them in.
    """
    plan_before = effective_limits(user).plan_id
    renew(user, order.plan, order.days, now)
    if user.can_have_keys:
        enforcer = LimitEnforcer(now)
        enforcer.switch(user, enforcer.reason(user, effective_limits(user)))
    apply_speed_if_changed(["plan"] if plan_before != order.plan_id else [])
