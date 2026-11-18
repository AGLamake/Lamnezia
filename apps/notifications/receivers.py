"""Notifications for signals of other parts of the panel, connected in `NotificationsConfig`."""

from typing import Any

from django.contrib.auth.signals import user_logged_out
from django.db.models.signals import post_delete
from django.http import HttpRequest

from apps.accounts.models import User
from apps.notifications.events import Event
from apps.notifications.services.browser import BrowserSubscriptions
from apps.notifications.services.delivery import emit
from apps.notifications.services.trouble import forget_troubles
from apps.protocols.signals import keys_changed, keys_moved, keys_revoked, short_id_changed
from apps.servers.models import Server
from apps.traffic.signals import limit_reached


def on_limit_reached(sender: object, user: User, reason: str, **kwargs: Any) -> None:
    emit(Event.LIMIT_REACHED, {"user": user.username, "reason": str(reason)}, user=user)


def on_keys_changed(sender: object, user: User, server: str, **kwargs: Any) -> None:
    emit(Event.KEYS_CHANGED, {"user": user.username, "server": server}, user=user)


def on_keys_revoked(sender: object, user: User, server: str, **kwargs: Any) -> None:
    emit(Event.KEYS_REVOKED, {"user": user.username, "server": server}, user=user)


def on_keys_moved(sender: object, user: User, server: str, **kwargs: Any) -> None:
    emit(Event.KEYS_MOVED, {"user": user.username, "server": server}, user=user)


def on_short_id_changed(sender: object, user: User, server: str, **kwargs: Any) -> None:
    emit(Event.SHORT_ID_CHANGED, {"user": user.username, "server": server}, user=user)


def on_server_deleted(sender: object, instance: Server, **kwargs: Any) -> None:
    """A deleted server or relay leaves no trouble behind in the list of troubles."""
    for event in (Event.SERVER_DOWN, Event.RELAY_DOWN, Event.AWG3_DROPPED):
        forget_troubles(f"{event}:{instance.pk}:")


def on_logged_out(sender: object, request: HttpRequest, user: User | None, **kwargs: Any) -> None:
    """The browser signs out: it stops getting the user's notifications."""
    endpoint = request.session.get(BrowserSubscriptions.SESSION_KEY)
    if user is not None and endpoint:
        BrowserSubscriptions(user).remove(endpoint)


def connect() -> None:
    user_logged_out.connect(on_logged_out, dispatch_uid="notifications.logged_out")
    post_delete.connect(on_server_deleted, sender=Server, dispatch_uid="notifications.server")
    limit_reached.connect(on_limit_reached, dispatch_uid="notifications.limit_reached")
    keys_changed.connect(on_keys_changed, dispatch_uid="notifications.keys_changed")
    keys_revoked.connect(on_keys_revoked, dispatch_uid="notifications.keys_revoked")
    keys_moved.connect(on_keys_moved, dispatch_uid="notifications.keys_moved")
    short_id_changed.connect(on_short_id_changed, dispatch_uid="notifications.short_id_changed")
