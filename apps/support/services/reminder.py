"""A reminder of the staff's answer in the user's own channel (DESIGN.md 6.7, 6.8).

The answer reaches the user where they last wrote. If they neither answer nor read it within
SUPPORT_REMINDER_DELAY, their chosen channel gets one short notice with a link to the thread,
unless the answer itself reached them there; the channel it reached is never reminded. An answer
the messenger did not take (the bot is blocked) does not count as reached. Answers in a row before
the user's next message make one reminder.
"""

from django.conf import settings
from django.urls import reverse

from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.notifications.services.delivery import emit
from apps.support.models import Conversation, Kind, Message


def answer_key(answer: Message) -> str:
    """The key of the notification that carries the answer itself to a messenger."""
    return f"support-answer-{answer.pk}"


class AnswerReminder:
    """The reminder of one answer."""

    def __init__(self, answer: Message) -> None:
        self._answer = answer

    def send(self) -> None:
        """Remind the user unless they already have the answer."""
        conversation = self._answer.conversation
        user = conversation.user
        reached = self._reached()
        if self._answer.read_at is not None or reached == user.notify_via:
            return
        first = self._first_unanswered(conversation)
        if first is None or first > self._answer.pk:
            return  # the user wrote after this answer
        if conversation.kind == Kind.TICKET:
            event = Event.REQUEST_REMINDER
            path = reverse("support:conversation", args=[conversation.pk])
            data = {"topic": conversation.subject}
        else:
            event, path, data = Event.SUPPORT_ANSWER, reverse("support:chat"), {}
        emit(
            event,
            {"user": user.username, **data, "url": settings.SITE_URL.rstrip("/") + path},
            key=f"support-reminder-{first}",
            user=user,
            skip_via=reached,
        )

    def _reached(self) -> str:
        """The user's channel the answer itself reached; empty when it reached none."""
        sent_via: str | None = (
            Notification.objects.filter(key=answer_key(self._answer))
            .values_list("sent_via", flat=True)
            .first()
        )
        return sent_via or ""

    @staticmethod
    def _first_unanswered(conversation: Conversation) -> int | None:
        """The first answer of the staff since the user's last message."""
        answers = conversation.messages.filter(internal=False).exclude(author=conversation.user_id)
        last_own = (
            conversation.messages.filter(author=conversation.user_id)
            .order_by("-pk")
            .values_list("pk", flat=True)
            .first()
        )
        if last_own is not None:
            answers = answers.filter(pk__gt=last_own)
        first: int | None = answers.order_by("pk").values_list("pk", flat=True).first()
        return first
