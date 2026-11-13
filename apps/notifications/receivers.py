"""Notifications for signals of other parts of the panel, connected in `NotificationsConfig`."""

from typing import Any

from apps.accounts.models import User
from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.protocols.signals import keys_changed, keys_moved, keys_revoked
from apps.traffic.signals import limit_reached


def on_limit_reached(sender: object, user: User, reason: str, **kwargs: Any) -> None:
    emit(Event.LIMIT_REACHED, {"user": user.username, "reason": str(reason)}, user=user)


def on_keys_changed(sender: object, user: User, server: str, **kwargs: Any) -> None:
    emit(Event.KEYS_CHANGED, {"user": user.username, "server": server}, user=user)


def on_keys_revoked(sender: object, user: User, server: str, **kwargs: Any) -> None:
    emit(Event.KEYS_REVOKED, {"user": user.username, "server": server}, user=user)


def on_keys_moved(sender: object, user: User, server: str, **kwargs: Any) -> None:
    emit(Event.KEYS_MOVED, {"user": user.username, "server": server}, user=user)


def connect() -> None:
    limit_reached.connect(on_limit_reached, dispatch_uid="notifications.limit_reached")
    keys_changed.connect(on_keys_changed, dispatch_uid="notifications.keys_changed")
    keys_revoked.connect(on_keys_revoked, dispatch_uid="notifications.keys_revoked")
    keys_moved.connect(on_keys_moved, dispatch_uid="notifications.keys_moved")
