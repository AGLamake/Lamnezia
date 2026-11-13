"""Live updates of support threads (DESIGN.md 6.7).

A change is announced to Channels groups as a bare signal: "a thread changed" or "the unread
count changed". An open page then fetches what changed over HTTP, through the same views and
permission checks as a page load, so no message text ever travels through the channel layer.
"""

from enum import StrEnum

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from apps.support.models import Conversation, Kind

STAFF_GROUP = "support.staff"


class Signal(StrEnum):
    THREAD = "thread"
    UNREAD = "unread"
    INBOX = "inbox"


def user_group(user_id: int) -> str:
    return f"support.user.{user_id}"


def thread_group(conversation_id: int) -> str:
    return f"support.thread.{conversation_id}"


def thread_changed(conversation: Conversation, *, staff_only: bool = False) -> None:
    """New message, read mark or status: the thread is to be fetched again.

    `staff_only` changes (internal notes) are not announced to the user of the thread. The chat
    is open in the widget of any portal page, so its changes reach every page of its user; the
    widget does not follow the chat as the page's thread.
    """
    _publish(thread_group(conversation.pk), Signal.THREAD, staff_only=staff_only)
    if conversation.kind == Kind.CHAT and not staff_only:
        _publish(user_group(conversation.user_id), Signal.THREAD)


def unread_changed(conversation: Conversation, *, staff: bool) -> None:
    """The unread count of the staff (`staff`) or of the user of the thread changed."""
    _publish(STAFF_GROUP if staff else user_group(conversation.user_id), Signal.UNREAD)


def inbox_changed() -> None:
    """The staff answered: the lists of conversations of the other staff members show it."""
    _publish(STAFF_GROUP, Signal.INBOX)


def _publish(group: str, signal: Signal, *, staff_only: bool = False) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    event = {"type": "support.signal", "signal": signal.value, "staff_only": staff_only}
    # After the commit: a page that fetches on the signal must see the change.
    transaction.on_commit(lambda: async_to_sync(layer.group_send)(group, event))
