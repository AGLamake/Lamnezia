"""Who may follow which thread live: the same people who may open it."""

from apps.accounts.models import User
from apps.support.models import Conversation
from apps.support.services.inbox import Inbox
from apps.support.services.live import STAFF_GROUP, thread_group, user_group


def follow(user: User, conversation_id: int | None = None) -> list[str] | None:
    """Groups whose signals reach `user`; None when the thread is not one they may read."""
    groups = [user_group(user.pk)]
    if user.is_staff_member:
        groups.append(STAFF_GROUP)
    if conversation_id is None:
        return groups
    if not _may_read(user, conversation_id):
        return None
    return [*groups, thread_group(conversation_id)]


def _may_read(user: User, conversation_id: int) -> bool:
    if Conversation.objects.filter(pk=conversation_id, user=user).exists():
        return True
    return user.is_staff_member and Inbox(user).visible().filter(pk=conversation_id).exists()
