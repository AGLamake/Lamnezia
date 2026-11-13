"""The staff's answers and status changes reach the user (DESIGN.md 6.7, 6.8).

Of a request, the user's chat gets a line with a link to it (`MyChat.tell`). A user who last
wrote in a thread from a messenger gets the answer itself there, from the bot, whether or not they
have the panel open; otherwise it waits in the panel. An answer the user does
not take up in SUPPORT_REMINDER_DELAY brings a reminder to their own channel (`AnswerReminder`).
"""

from django.conf import settings
from django.db import transaction
from django.utils import translation
from django.utils.translation import gettext as _

from apps.notifications.events import Event, english
from apps.notifications.services.delivery import emit
from apps.support.models import Conversation, Kind, Message, TicketEvent, TicketStatus
from apps.support.services.chat import MyChat
from apps.support.services.reminder import answer_key
from apps.support.tasks import remind_of_answer

# The statuses a user hears of; `in_progress` comes with the answer itself.
_TOLD_STATUSES = frozenset({TicketStatus.WAITING_USER, TicketStatus.RESOLVED, TicketStatus.CLOSED})


def last_via(conversation: Conversation) -> str:
    """The messenger of the user's latest message in the thread; empty: the panel."""
    via: str | None = (
        conversation.messages.filter(author=conversation.user_id, internal=False)
        .order_by("-pk")
        .values_list("via", flat=True)
        .first()
    )
    return via or ""


def tell_of_answer(conversation: Conversation, answer: Message) -> None:
    via = last_via(conversation)
    countdown = int(settings.SUPPORT_REMINDER_DELAY.total_seconds())
    transaction.on_commit(lambda: remind_of_answer.apply_async((answer.pk,), countdown=countdown))
    if conversation.kind == Kind.TICKET:
        MyChat(conversation.user).tell(conversation, TicketEvent.ANSWER)
    if via:
        _relay(conversation, answer, via)


def _relay(conversation: Conversation, answer: Message, via: str) -> None:
    """The answer itself, from the bot of the messenger `via`."""
    text = answer.text
    if answer.attachments.exists():
        with translation.override(conversation.user.notification_language):
            note = _("A picture is attached: open Support in your personal account to see it.")
        text = f"{text}\n\n{note}" if text else note
    request = conversation.kind == Kind.TICKET
    emit(
        Event.REQUEST_ANSWER if request else Event.CHAT_ANSWER,
        {
            "user": conversation.user.username,
            "topic": conversation.subject or "—",
            "text": text,
        },
        key=answer_key(answer),
        user=conversation.user,
        via=via,
    )


def tell_of_status(conversation: Conversation) -> None:
    """A request waits for the user, is resolved or closed: the user hears of it."""
    if conversation.kind != Kind.TICKET or conversation.status not in _TOLD_STATUSES:
        return
    MyChat(conversation.user).tell(conversation, TicketEvent(conversation.status))
    emit(
        Event.REQUEST_STATUS,
        {
            "user": conversation.user.username,
            "topic": conversation.subject or "—",
            "status": english(TicketStatus(conversation.status).label),
        },
        user=conversation.user,
        via=last_via(conversation),
    )
