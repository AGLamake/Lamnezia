"""Notifications about signing in: from a new place to the user, a lockout to the staff.

Neither a password nor a 2FA code is ever among the details: a lockout tells only the name typed
in and the address.
"""

from datetime import timedelta

from django.conf import settings
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services.sessions import device_name
from apps.accounts.services.sign_in_places import SignInPlaces
from apps.audit.services.journal import client_ip
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.notifications.services.delivery import emit


class SignInAlerts:
    """Turns sign-ins and lockouts into notifications."""

    def signed_in(self, user: User, request: HttpRequest) -> None:
        """Remember where `user` signed in from; a new place is news when they want to hear it."""
        place = SignInPlaces(user).visit(
            client_ip(request), device_name(request.headers.get("User-Agent", ""))
        )
        if place is None or not user.wants_sign_in_alerts:
            return
        data = {
            "user": user.username,
            "device": place.device,
            "url": settings.SITE_URL.rstrip("/") + reverse("accounts:profile") + "#sessions",
        }
        if place.address:
            data["address"] = place.address
        emit(Event.SIGN_IN_NEW_PLACE, data, user=user)

    def locked_out(self, username: str, address: str | None) -> None:
        """Tell the staff once per lockout: django-axes reports every refused try while it lasts."""
        cooloff: timedelta = settings.AXES_COOLOFF_TIME
        details = {"user": username, "address": address or "—"}
        repeated = Notification.objects.filter(
            event=Event.SIGN_IN_LOCKED,
            data__user=username,
            data__address=details["address"],
            created_at__gte=timezone.now() - cooloff,
        ).exists()
        if repeated:
            return
        emit(
            Event.SIGN_IN_LOCKED,
            details
            | {
                "failures": settings.AXES_FAILURE_LIMIT,
                "minutes": int(cooloff.total_seconds() // 60),
            },
        )
