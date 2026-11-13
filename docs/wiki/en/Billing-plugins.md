# Billing plugins

The panel does not take payments by itself. A payment service (a bank, a payment aggregator, a
crypto gateway) is connected with a plugin: a small Django app that implements one class. Without
plugins the panel works as before and the **Payment** page is hidden.

[Русская версия](../ru/Billing-plugins.md)

## How paying works

1. The administrator gives a plan a price and a currency ([plans](Users-plans-limits.md#plans)).
   Plans with a price, a currency and not marked as a trial are on sale.
2. An approved user opens **Payment** in the portal, chooses a plan and a way to pay.
3. The panel creates an order. It keeps the price, the currency and the term of the plan at that
   moment, so editing the plan later does not change orders already made.
4. The plugin registers the order with the payment service and returns the address of its payment
   page. The user pays there and comes back to the order page in the panel.
5. The payment service reports the result to the notify address of the panel. The plugin checks
   that the report is genuine and reads it, the panel checks the amount and the currency against the
   order and renews access.

Renewal follows the rules of the **Extend access** bulk action:

- paying for the current plan moves the end of access later by the term of the plan, counting from
  the current end, or from today if access has already ended;
- another plan replaces the current one, its term starts today, a personal end of access is
  replaced, other personal limits stay;
- a plan without a term gives access with no end date.

Keys switched off because access ended come back on at once, without waiting for the next limit
check. A report that comes twice is applied once. A report of a failed payment marks the order as
not paid, and a successful report for the same order later still counts. The **Audit log** records
`billing.checkout`, `billing.paid` and `billing.refunded`.

## Orders in the panel

Administrators see every order on the **Orders** page of the admin interface (`/billing/orders/`,
shown while a plugin is on), with filters by status, payment method, user and date. Statuses:

- **waiting for payment**: the user went to the payment page and the service has not reported yet;
- **paid**: the payment came and access is renewed;
- **not paid**: the service refused the order or reported a failed payment;
- **expired**: no report came within a day of the order; the user can simply buy again. A payment
  reported later still counts: money that came is never turned away;
- **refunded**: an administrator returned the money (see below).

Every payment is also the **Payment received** event: route it to a staff channel on the
**Notifications** page ([notifications](Notifications.md)).

Orders outlive their users. When a user is deleted, their orders stay in the list with the username
they had, marked *deleted*; a payment for such an order is recorded and renews nothing.

**Blocked users.** A user waiting for approval or blocked cannot start a payment. If the account is
blocked after the order was made, the payment that comes is still recorded and the term of the plan
is given, but no keys are turned on while the account is blocked. The staff hear of the payment and
decide: let the user in or return the money.

**Refunds.** If the plugin can return money, a paid order has **Refund** in the list. The panel asks
the payment service to return the money and marks the order as refunded; the plan and the term of
the user stay as they are, so change them on the user card if needed. A refunded order is never
paid again by a repeated report.

## Turning a plugin on

The plugin is a Python package installed next to the panel (in your own image or a mounted
directory). List it in `.env`:

```env
LAMNEZIA_PLUGINS=my_payments
```

Several plugins are separated by commas. They are added to `INSTALLED_APPS`, so a plugin can have
its own models, migrations, templates and translations. After changing the list:
`docker compose up -d` and, if the plugin has models, `docker compose exec web python manage.py migrate`.

## Writing a plugin

A plugin registers a subclass of `PaymentProvider` from `apps.billing.services.providers` in
`AppConfig.ready()`:

```python
from django.apps import AppConfig


class MyPaymentsConfig(AppConfig):
    name = "my_payments"

    def ready(self) -> None:
        from apps.billing.services.providers import payment_providers
        from my_payments.provider import MyProvider

        payment_providers.register(MyProvider())
```

What the class gives:

| Member | Required | Meaning |
|---|---|---|
| `key` | yes | Stable short name, letters, digits and `-`. It is saved in orders and is part of addresses: do not change it once orders exist. |
| `title` | yes | Name of the way to pay shown to users, through gettext. |
| `start(order, urls)` | yes | Registers the order with the payment service and returns the address to send the user to. May set `order.external_id`; the panel saves it. Raise `PaymentProviderError` if the service refuses: the order is marked as not paid and the user sees "try again later". |
| `parse_notification(request)` | yes | Checks that the request comes from the payment service and returns a `PaymentNotice`. Raise `InvalidNotificationError` if it is not genuine or cannot be read: the panel answers `400`. |
| `acknowledge(request, order)` | no | Answer to an applied report. By default `200 OK` as plain text; some services want JSON or a redirect. |
| `is_available()` | no | `False` hides the plugin, for example while its settings are not filled in. |
| `urls()` | no | Pages of the plugin, served under `/billing/<key>/` in the URL namespace `billing:<key>`. |
| `refunds`, `refund(order)` | no | A plugin that can return money sets `refunds = True` and implements `refund`: it asks the service to return the money of a paid order. Raise `PaymentProviderError` if the service refuses: the order stays paid and the administrator sees "try again later". |

`CheckoutUrls` passed to `start` holds two absolute addresses:

- `return_url`: the order page in the panel, where the user comes back after paying;
- `notify_url`: `https://<domain>/billing/notify/<key>/`, where the service reports the result.
  It accepts `POST` without a CSRF token and without signing in.

`PaymentNotice` holds `order_number` (the `order.number` UUID you sent to the service), `paid`,
`amount` and `currency` actually paid, and the payment id in the service (`external_id`).

### Security

- **Check every report.** The notify address is open to anyone. Verify the signature or the shared
  secret of the service, or ask the service back about the payment, before you trust any field.
- **Report what was paid**, not what was ordered. The panel refuses a report whose amount or
  currency differs from the order (answer `400`), so a forged or partial payment does not renew
  access.
- Send the service `order.number`, not the database id: numbers cannot be guessed.
- Keep keys of the payment service in settings or environment variables, never in the code, and do
  not log them or the reports.
- The panel applies a report under a database lock, so reports that come at the same time or
  repeat do not extend access twice.

## The example plugin

`examples/billing_dummy` is a working plugin to read and to try the flow locally. Its payment page
lives in the panel and offers **Pay** and **Refuse**; it posts a report signed with the panel secret
key to the real notify address. It works only with `LAMNEZIA_DEBUG=True`, so it cannot be turned on
by mistake on a public panel:

```env
LAMNEZIA_DEBUG=True
LAMNEZIA_PLUGINS=examples.billing_dummy
```

Its tests in `examples/billing_dummy/tests` show the whole flow: an order, the payment page, the
report and the renewed access.
