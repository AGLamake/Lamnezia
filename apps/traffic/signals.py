"""Hooks for notifications. Both are sent after the transaction that started the change commits.

- `limit_reached(user, reason)`: the user's keys are being turned off; `reason` is a `LimitReason`.
- `access_restored(user)`: keys turned off by the limits are being turned back on.
"""

from django.db import models
from django.dispatch import Signal
from django.utils.translation import gettext_lazy as _


class LimitReason(models.TextChoices):
    """Why the limits turned the keys off; the value is what a notification webhook gets."""

    EXPIRED = "expired", _("the access term is over")
    TOTAL_TRAFFIC = "total_traffic", _("the traffic in total is used up")
    TRAFFIC = "traffic", _("the traffic of the period is used up")


limit_reached = Signal()
access_restored = Signal()
