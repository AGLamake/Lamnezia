"""Messages of a thread, shared by the user's side and the staff's."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.accounts.models import User
from apps.support.models import Attachment, Conversation, Message
from apps.support.services import live
from apps.support.services.images import Upload, validate_image


class TooManyMessagesError(Exception):
    pass


def post(  # noqa: PLR0913  (two keyword-only marks of the message)
    conversation: Conversation,
    author: User | None,
    text: str,
    image: Upload | None = None,
    *,
    internal: bool = False,
    via: str = "",
) -> Message:
    """Add a message to a thread; an `internal` note is shown to the staff only. `via` is the
    messenger the user wrote it in.
    """
    with transaction.atomic():
        message = Message.objects.create(
            conversation=conversation,
            author=author,
            text=text.strip(),
            internal=internal,
            via=via,
        )
        if image is not None:
            content_type = validate_image(image)
            Attachment.objects.create(
                message=message, file=image, content_type=content_type, size=image.size or 0
            )
        Conversation.objects.filter(pk=conversation.pk).update(updated_at=message.created_at)
    live.thread_changed(conversation, staff_only=internal)
    if not internal:
        from_user = author is not None and author.pk == conversation.user_id
        live.unread_changed(conversation, staff=from_user)
        if not from_user:
            live.inbox_changed()
    return message


class MyThreads:
    """Threads of one user with the staff, as the user sees them: no internal notes.

    The base of the user's chat and of the user's requests. `via` is the messenger the user
    writes from (empty: the panel); the staff's answers go back there.
    """

    def __init__(self, user: User, via: str = "") -> None:
        self._user = user
        self._via = via

    def all(self) -> QuerySet[Conversation]:
        return self._user.conversations.all()

    def messages(self, conversation: Conversation) -> QuerySet[Message]:
        return conversation.messages.filter(internal=False).prefetch_related("attachments")

    def unread(self) -> int:
        """Answers the user has not seen yet, for the menu."""
        return (
            Message.objects.filter(conversation__in=self.all(), internal=False, read_at=None)
            .exclude(author=self._user)
            .count()
        )

    def mark_read(self, conversation: Conversation) -> None:
        seen = (
            conversation.messages.filter(internal=False, read_at=None)
            .exclude(author=self._user)
            .update(read_at=timezone.now())
        )
        if seen:
            live.thread_changed(conversation)
            live.unread_changed(conversation, staff=False)

    def _post(self, conversation: Conversation, text: str, image: Upload | None = None) -> Message:
        """A message of the user, at most SUPPORT_MESSAGE_RATE_LIMIT a minute (0: no limit)."""
        limit = settings.SUPPORT_MESSAGE_RATE_LIMIT
        since = timezone.now() - timedelta(minutes=1)
        sent = Message.objects.filter(author=self._user, created_at__gte=since)
        if limit and sent.count() >= limit:
            raise TooManyMessagesError
        return post(conversation, self._user, text, image, via=self._via)
